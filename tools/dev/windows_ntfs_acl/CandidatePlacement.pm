package CandidatePlacement;
use strict;use warnings;use JSON::PP ();use Digest::SHA qw(sha256_hex);
use Errno qw(ENOENT EEXIST);use Fcntl qw(S_ISDIR S_ISREG);use Time::HiRes qw(clock_gettime CLOCK_MONOTONIC);
use RestrictedAcl;use CandidateInspection;

# This module authorizes nothing. A separately reviewed one-use root wrapper
# must bind candidate B, the approved ancestor evidence, payload and host state.
sub phases {
 return qw(configuration launch capability_root capability_tools capability_version capability_digest disk_binding mount_rw binding_rw ancestry absence create duplicate_create acl_precheck acl_set_rpc acl_set_returned acl_readback write verify remount_ro binding_ro verify_ro umount shutdown close);
}
sub collect {
 my($g,$work,$payload,$expected,$tool_sha,$clock)=@_;$clock//=sub {clock_gettime(CLOCK_MONOTONIC)};
 local $SIG{__WARN__}=sub {die 'warning'};
 my @fail;my $r={result=>'STOP',scope=>'candidate_b_unattend_placement',failure_phases=>\@fail,
  setter_exit=>undef,setter_rpc_errno=>undef,file_inode=>undef,
  map {$_=>JSON::PP::false} qw(ancestry_verified destination_absent exclusive_create_verified restricted_sd_before_write restricted_sd_after_write restricted_sd_after_remount payload_verified verification_remount_passed closure_confirmed appliance_may_be_running)};
 my($launched,$mounted,$created,$secured)=(0,0,0,0);my $phase='configuration';my $deadline=$clock->()+300;
 my $check=sub {die 'deadline' if $clock->()>$deadline};
 my $call=sub {my($m,@a)=@_;$check->();if(wantarray){my @v=$g->$m(@a);$check->();return @v}my $v=$g->$m(@a);$check->();return $v};
 my $pairs=sub {my($fields,@v)=@_;die 'shape' unless @v==2*@$fields;my %s;my %allowed=map {$_=>1} @$fields;
  while(@v){my($k,$v)=splice(@v,0,2);die 'field' unless defined($k)&&!ref($k)&&exists($allowed{$k})&&!exists($s{$k});$s{$k}=$v}return \%s;};
 my $stat=sub {my($path)=@_;my $s=$pairs->([CandidateInspection::statns_fields()],$call->('lstatns',$path));
  for my $k(qw(st_dev st_ino st_mode st_nlink st_size)){CandidateInspection::uint($s->{$k},$k eq 'st_mode'?65535:9223372036854775807)}return $s;};
 my $attribute=sub {my($path,$dir)=@_;my $a=$call->('lgetxattr',$path,'system.ntfs_attrib_be');
  die 'attribute_size' unless defined($a)&&!ref($a)&&length($a)==4;my $flags=unpack('N',$a);
  die 'attribute_type' if ($flags&0x400)||!!($flags&0x10)!=!!$dir;return $flags;};
 my $path='/Windows/Panther/unattend.xml';my $sd=RestrictedAcl::descriptor();my($ino,$parent_after);
 my $listing=sub {my($parent,$child,$count)=@_;my @names=$call->('ls',$parent);die 'listing_bound' if @names>10000;
  my @matches;for my $name(@names){die 'entry' unless defined($name)&&!ref($name)&&length($name)<=1024&&$name!~m{[/\x00]}&&$name ne '.'&&$name ne '..';push @matches,$name if lc($name) eq lc($child)}
  die 'case_binding' unless @matches==$count&&(!$count||$matches[0] eq $child);};
 my $ancestors=sub {
  for my $e(@$expected){my $s=$stat->($e->{path});die 'directory_type' unless S_ISDIR($s->{st_mode});
   for my $k(qw(st_dev st_ino st_mode)){die 'ancestor_binding' unless $s->{$k}==$e->{$k}}
   die 'ancestor_attribute' unless $attribute->($e->{path},1)==$e->{ntfs_flags};
   die 'ancestor_acl' unless $call->('lgetxattr',$e->{path},'system.ntfs_acl') eq pack('H*',$e->{sd_hex});
   if($created&&$e->{path} eq '/Windows/Panther'){
    # Only this parent's index allocation/link metadata may change from adding
    # the one child. Capture once, then require stability through RO remount.
    die 'parent_growth' unless $s->{st_nlink}>=$e->{st_nlink}&&$s->{st_nlink}<=$e->{st_nlink}+1&&$s->{st_size}>=$e->{st_size}&&$s->{st_size}<=$e->{st_size}+65536;
    $parent_after//=[$s->{st_nlink},$s->{st_size}];die 'parent_changed' unless $s->{st_nlink}==$parent_after->[0]&&$s->{st_size}==$parent_after->[1];
   }else{for my $k(qw(st_nlink st_size)){die 'ancestor_changed' unless $s->{$k}==$e->{$k}}}
  }
  $listing->('/','Windows',1);$listing->('/Windows','Panther',1);
 };
 my $file=sub {my($size)=@_;$ancestors->();$listing->('/Windows/Panther','unattend.xml',1);my $s=$stat->($path);
  die 'file_binding' unless S_ISREG($s->{st_mode})&&!($s->{st_mode}&07000)&&($secured||($s->{st_mode}&07777)==0600)
   &&$s->{st_dev}==2051&&$s->{st_ino}>5&&$s->{st_nlink}==1&&$s->{st_size}==$size;
  die 'file_alias' if grep {$_->{st_ino}==$s->{st_ino}} @$expected;
  die 'file_changed' if defined($ino)&&$ino!=$s->{st_ino};$ino=$s->{st_ino};$r->{file_inode}=$ino;$attribute->($path,0);
  die 'descriptor' if $secured&&$call->('lgetxattr',$path,'system.ntfs_acl') ne $sd;
 };
 my $verify=sub {$file->(length($payload));die 'payload' unless $call->('pread',$path,length($payload)+1,0) eq $payload;$file->(length($payload))};
 my $binding=sub {my($ro)=@_;my @m=$call->('mountpoints');die 'mount' unless @m==2&&$m[0] eq '/dev/sda3'&&$m[1] eq '/';
  my $v=$pairs->([CandidateInspection::statvfs_fields()],$call->('statvfs','/'));my $flag=CandidateInspection::uint($v->{flag},9223372036854775807);die 'access' unless $flag>0&&!!($flag&1)==!!$ro;};
 eval {
  die 'workspace' unless defined($work)&&!ref($work)&&$work=~m{\A/var/lib/libvirt/images/astinuslab200/unattended-b-placement-[0-9]{8}(?:-r[1-9][0-9]{0,2})?\z};
  die 'payload_bound' unless defined($payload)&&!ref($payload)&&length($payload)>0&&length($payload)<=16384&&$payload=~/\A<\?xml /&&$payload!~/\x00/;
  die 'evidence_shape' unless ref($expected) eq 'ARRAY'&&@$expected==3;my @paths=('/','/Windows','/Windows/Panther');
  for my $i(0..2){my $e=$expected->[$i];die 'evidence_path' unless ref($e) eq 'HASH'&&$e->{path} eq $paths[$i];
   for my $k(qw(st_dev st_ino st_mode st_nlink st_size ntfs_flags)){CandidateInspection::uint($e->{$k},9223372036854775807)}
   die 'evidence_binding' unless $e->{st_dev}==2051&&S_ISDIR($e->{st_mode})&&$e->{st_mode}<=65535&&$e->{st_nlink}>0&&$e->{st_size}<=67108864&&($i==0?$e->{st_ino}==5:$e->{st_ino}>5);
   die 'evidence_attribute' unless $e->{ntfs_flags}<=4294967295&&($e->{ntfs_flags}&0x10)&&!($e->{ntfs_flags}&0x400);
   die 'evidence_acl' unless defined($e->{sd_hex})&&!ref($e->{sd_hex})&&$e->{sd_hex}=~/\A(?:[0-9a-f]{2}){20,8192}\z/
    &&sha256_hex(pack('H*',$e->{sd_hex})) eq $e->{sd_sha256};
  }
  my @caps=RestrictedAcl::capability_checks($tool_sha);
  $call->('set_backend','libvirt:qemu:///system');$call->('set_backend_settings',[]);$call->('set_network',0);$call->('set_trace',0);$call->('set_verbose',0);
  $call->('set_cachedir',"$work/cache");$call->('set_tmpdir',"$work/tmp");
  $call->('add_drive_opts','/var/lib/libvirt/images/astinuslab200/wintrial-2022-20260913-unattended-b/disk.qcow2',readonly=>0,format=>'qcow2');
  $phase='launch';$launched=1;$call->('launch');
  for my $c(@caps){$phase=$c->[0];die 'capability' unless $call->('debug','sh',[$c->[1]]) eq $c->[2]}
  $phase='disk_binding';my @dev=$call->('list_devices');my @parts=$call->('list_partitions');
  die 'devices' unless @dev==1&&$dev[0] eq '/dev/sda'&&join(',',@parts) eq '/dev/sda1,/dev/sda2,/dev/sda3';
  die 'layout' unless $call->('part_get_parttype','/dev/sda') eq 'gpt'&&$call->('blockdev_getsize64','/dev/sda')==42949672960&&$call->('blockdev_getsize64','/dev/sda3')==41875931136&&$call->('vfs_type','/dev/sda3') eq 'ntfs';
  die 'probe_rw' unless $call->('ntfs_3g_probe',1,'/dev/sda3')==0;
  $phase='mount_rw';$mounted=1;$call->('mount_vfs','rw,norecover,posix_nlink,no_def_opts,windows_names,permissions','ntfs-3g','/dev/sda3','/');
  $phase='binding_rw';$binding->(0);$phase='ancestry';$ancestors->();$r->{ancestry_verified}=JSON::PP::true;
  $phase='absence';$listing->('/Windows/Panther','unattend.xml',0);$check->();my $exists=eval {my @v=$g->lstatns($path);1};my $errno=$exists?0:CandidateInspection::uint($g->last_errno(),4095);$check->();die 'absence' unless !$exists&&$errno==ENOENT;$r->{destination_absent}=JSON::PP::true;
  $phase='create';$ancestors->();$call->('mknod',0100600,0,0,$path);$created=1;$file->(0);
  $phase='duplicate_create';$check->();my $ok=eval {$g->mknod(0100600,0,0,$path);1};$errno=$ok?0:CandidateInspection::uint($g->last_errno(),4095);$check->();die 'exclusive' unless !$ok&&$errno==EEXIST;$file->(0);$r->{exclusive_create_verified}=JSON::PP::true;
  $phase='acl_precheck';$file->(0);$phase='acl_set_rpc';my $reply;my $set_ok=eval {$reply=$call->('debug','sh',[RestrictedAcl::setter_command($path)]);1};
  unless($set_ok){$r->{setter_rpc_errno}=CandidateInspection::uint($g->last_errno(),4095);die 'setter_rpc'}
  $phase='acl_set_returned';$r->{setter_exit}=RestrictedAcl::setter_exit($reply);die 'setter_exit' unless $r->{setter_exit}==0;
  $phase='acl_readback';$secured=1;$file->(0);$r->{restricted_sd_before_write}=JSON::PP::true;
  $phase='write';$file->(0);die 'short_write' unless $call->('pwrite',$path,$payload,0)==length($payload);
  $phase='verify';$verify->();$r->{payload_verified}=JSON::PP::true;$r->{restricted_sd_after_write}=JSON::PP::true;
  $phase='remount_ro';$call->('sync');$call->('umount_all');$mounted=0;my @m=$call->('mountpoints');die 'still_mounted' if @m;
  die 'probe_ro' unless $call->('ntfs_3g_probe',0,'/dev/sda3')==0;$mounted=1;$call->('mount_vfs','ro,norecover,posix_nlink,no_def_opts,windows_names,permissions','ntfs-3g','/dev/sda3','/');
  $phase='binding_ro';$binding->(1);$phase='verify_ro';$verify->();$r->{restricted_sd_after_remount}=JSON::PP::true;$r->{verification_remount_passed}=JSON::PP::true;
  1;
 } or push @fail,$phase;
 my $cleanup=sub {my($label,$method)=@_;my $ok=eval {$g->$method();1};push @fail,$label unless $ok;return $ok};
 my $unmounted=$mounted?$cleanup->('umount','umount_all'):1;my $stopped=$launched?$cleanup->('shutdown','shutdown'):1;my $closed=$cleanup->('close','close');
 $r->{closure_confirmed}=$unmounted&&$stopped&&$closed?JSON::PP::true:JSON::PP::false;
 $r->{appliance_may_be_running}=$launched&&!($stopped&&$closed)?JSON::PP::true:JSON::PP::false;
 $r->{result}='CANDIDATE_UNATTEND_PLACED' unless @fail;return $r;
}
1;
