package CandidateInspection;
use strict;use warnings;use JSON::PP ();use Digest::SHA qw(sha256_hex);
use Errno qw(ENOENT);use Fcntl qw(S_ISDIR);use Time::HiRes qw(clock_gettime CLOCK_MONOTONIC);

# Metadata collection only. The host wrapper must create and compare a NEW
# standalone copy, bind its identity, and preserve the original candidate.
sub uint {
 my($v,$max)=@_;die 'numeric' unless defined($v)&&!ref($v)&&$v=~/\A(0|[1-9][0-9]*)\z/&&length($v)<=19&&$v<=$max;return 0+$v;
}
sub statns_fields {
 return qw(st_dev st_ino st_mode st_nlink st_uid st_gid st_rdev st_size st_blksize st_blocks st_atime_sec st_atime_nsec st_mtime_sec st_mtime_nsec st_ctime_sec st_ctime_nsec st_spare1 st_spare2 st_spare3 st_spare4 st_spare5 st_spare6);
}
sub statvfs_fields {return qw(bsize frsize blocks bfree bavail files ffree favail fsid flag namemax)}
sub phases {
 return ('configuration','launch','disk_binding','mount_ro','readonly',
  (map {my $p=$_;map {"$p$_"} qw(root_stat root_binding root_attribute root_acl windows_lookup windows_stat windows_binding windows_attribute windows_acl panther_lookup panther_stat panther_binding panther_attribute panther_acl destination_lookup destination_stat)} ('','repeat_')),
  'repeat_compare','umount','shutdown','close');
}
sub collect {
 my($g,$root,$clock)=@_;$clock//=sub {clock_gettime(CLOCK_MONOTONIC)};local $SIG{__WARN__}=sub {die 'warning'};
 my @fail;my $r={result=>'STOP',scope=>'candidate_copy_readonly_metadata',failure_phases=>\@fail,directories=>[],
  closure_confirmed=>JSON::PP::false,appliance_may_be_running=>JSON::PP::false,last_stat=>undef,
  destination_absent=>JSON::PP::false,repeat_matches=>JSON::PP::false,mount_readonly=>JSON::PP::false};
 my($launched,$mounted)=(0,0);my $phase='configuration';my $deadline=$clock->()+300;
 my $check=sub {die 'deadline' if $clock->()>$deadline};
 my $call=sub {my($m,@a)=@_;$check->();if(wantarray){my @v=$g->$m(@a);$check->();return @v}my $v=$g->$m(@a);$check->();return $v};
 my $pairs=sub {my($fields,@v)=@_;die 'shape' unless @v==2*@$fields;my %s;my %allowed=map {$_=>1} @$fields;
  while(@v){my($k,$v)=splice(@v,0,2);die 'field' unless defined($k)&&!ref($k)&&exists($allowed{$k})&&!exists($s{$k});$s{$k}=$v}return \%s;};
 my $directory=sub {
  my($path,$label)=@_;$phase=$label.'_stat';my $s=$pairs->([statns_fields()],$call->('lstatns',$path));
  for my $k(qw(st_dev st_ino st_mode st_nlink st_size)){uint($s->{$k},$k eq 'st_mode'?65535:9223372036854775807)}
  $r->{last_stat}={path=>$path,map {$_=>0+$s->{$_}} qw(st_dev st_ino st_mode st_nlink st_size)};
  $phase=$label.'_binding';
  die 'directory' unless S_ISDIR($s->{st_mode})&&$s->{st_dev}==2051&&$s->{st_nlink}>=1&&$s->{st_size}<=67108864
   &&($path eq '/'?$s->{st_ino}==5:$s->{st_ino}>5);
  $phase=$label.'_attribute';my $a=$call->('lgetxattr',$path,'system.ntfs_attrib_be');die 'attribute_length' unless defined($a)&&!ref($a)&&length($a)==4;
  my $flags=unpack('N',$a);die 'reparse_or_type' unless ($flags&0x10)&&!($flags&0x400);
  $phase=$label.'_acl';my $sd=$call->('lgetxattr',$path,'system.ntfs_acl');die 'acl_bound' unless defined($sd)&&!ref($sd)&&length($sd)>=20&&length($sd)<=8192;
  return {path=>$path,(map {$_=>0+$s->{$_}} qw(st_dev st_ino st_mode st_nlink st_size)),ntfs_flags=>$flags,
   sd_hex=>unpack('H*',$sd),sd_sha256=>sha256_hex($sd)};
 };
 my $child=sub {
  my($parent,$name,$absent)=@_;my @names=$call->('ls',$parent);die 'listing_bound' if @names>10000;my @matches;
  for my $entry(@names){die 'entry' unless defined($entry)&&!ref($entry)&&length($entry)<=1024&&$entry!~m{[/\x00]}&&$entry ne '.'&&$entry ne '..';push @matches,$entry if lc($entry) eq lc($name)}
  die 'ambiguous_or_missing' unless @matches==($absent?0:1);
  my $part=$absent?$name:$matches[0];die 'path_case' unless $part=~/\A[A-Za-z.]+\z/;
  return ($parent eq '/'?'':$parent).'/'.$part;
 };
 my $observe=sub {
  my($rows,$prefix)=@_;push @$rows,$directory->('/',$prefix.'root');
  $phase=$prefix.'windows_lookup';my $windows=$child->('/','Windows',0);push @$rows,$directory->($windows,$prefix.'windows');
  $phase=$prefix.'panther_lookup';my $panther=$child->($windows,'Panther',0);push @$rows,$directory->($panther,$prefix.'panther');
  $phase=$prefix.'destination_lookup';
  my $path=$child->($panther,'unattend.xml',1);
  $phase=$prefix.'destination_stat';
  $check->();my $exists=eval {my @s=$g->lstatns($path);1};my $errno=$exists?0:uint($g->last_errno(),4095);$check->();
  die 'destination_absence' unless !$exists&&$errno==ENOENT;
 };
 eval {
  die 'workspace' unless defined($root)&&!ref($root)&&$root=~m{\A/var/lib/libvirt/images/astinuslab200/unattended-b-audit-[0-9]{8}(?:-r[1-9][0-9]{0,2})?\z};
  $call->('set_backend','libvirt:qemu:///system');$call->('set_backend_settings',[]);
  $call->('set_network',0);$call->('set_trace',0);$call->('set_verbose',0);
  $call->('set_cachedir',"$root/cache");$call->('set_tmpdir',"$root/tmp");
  $call->('add_drive_opts',"$root/inspection.qcow2",readonly=>1,format=>'qcow2');
  $phase='launch';$launched=1;$call->('launch');
  $phase='disk_binding';my @dev=$call->('list_devices');my @parts=$call->('list_partitions');
  die 'devices' unless @dev==1&&$dev[0] eq '/dev/sda'&&join(',',@parts) eq '/dev/sda1,/dev/sda2,/dev/sda3';
  die 'layout' unless $call->('part_get_parttype','/dev/sda') eq 'gpt'
   &&$call->('blockdev_getsize64','/dev/sda')==42949672960&&$call->('blockdev_getsize64','/dev/sda3')==41875931136;
  die 'ntfs' unless $call->('vfs_type','/dev/sda3') eq 'ntfs';
  die 'probe' unless $call->('ntfs_3g_probe',0,'/dev/sda3')==0;
  $phase='mount_ro';$mounted=1;$call->('mount_vfs','ro,norecover,posix_nlink,no_def_opts,windows_names,permissions','ntfs-3g','/dev/sda3','/');
  $phase='readonly';my @mounts=$call->('mountpoints');die 'mount' unless @mounts==2&&$mounts[0] eq '/dev/sda3'&&$mounts[1] eq '/';
  my $v=$pairs->([statvfs_fields()],$call->('statvfs','/'));die 'writable' unless uint($v->{flag},9223372036854775807)&1;$r->{mount_readonly}=JSON::PP::true;
  $observe->($r->{directories},'');$r->{destination_absent}=JSON::PP::true;
  my $second=[];$observe->($second,'repeat_');my $json=JSON::PP->new->canonical;
  $phase='repeat_compare';
  die 'changed' unless $json->encode($second) eq $json->encode($r->{directories});$r->{repeat_matches}=JSON::PP::true;
  1;
 } or push @fail,$phase;
 my $cleanup=sub {my($label,$method)=@_;my $ok=eval {$g->$method();1};push @fail,$label unless $ok;return $ok};
 my $unmounted=$mounted?$cleanup->('umount','umount_all'):1;my $stopped=$launched?$cleanup->('shutdown','shutdown'):1;my $closed=$cleanup->('close','close');
 $r->{closure_confirmed}=$unmounted&&$stopped&&$closed?JSON::PP::true:JSON::PP::false;
 $r->{appliance_may_be_running}=$launched&&!($stopped&&$closed)?JSON::PP::true:JSON::PP::false;
 $r->{result}='CANDIDATE_METADATA_RECORDED' unless @fail;return $r;
}
1;
