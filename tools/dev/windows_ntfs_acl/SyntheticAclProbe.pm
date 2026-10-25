package SyntheticAclProbe;
use strict;use warnings;use JSON::PP ();use Errno qw(EEXIST ENOENT);use Fcntl qw(S_ISREG S_ISDIR);
use Time::HiRes qw(clock_gettime CLOCK_MONOTONIC);
use RestrictedAcl;
sub uint {
 my($v,$max)=@_;die 'numeric' unless defined($v)&&!ref($v)&&$v=~/^(0|[1-9][0-9]*)$/&&length($v)<=19&&$v<=$max;return 0+$v;
}
sub descriptor {
 return RestrictedAcl::descriptor();
}
sub collect {
 my($g,$root,$clock,$tool_sha)=@_;$clock//=sub {clock_gettime(CLOCK_MONOTONIC)};local $SIG{__WARN__}=sub {die 'warning'};
 my @fail;my $r={result=>'STOP',scope=>'synthetic_ntfs_acl_persistence',failure_phases=>\@fail,
  setter_exit=>undef,setter_rpc_errno=>undef,
  fixture_capacity=>134217728,closure_confirmed=>JSON::PP::false,appliance_may_be_running=>JSON::PP::false,
  map {$_=>JSON::PP::false} qw(precreate_binding_verified exclusive_create_verified payload_verified verification_remount_passed restricted_sd_before_write restricted_sd_after_write restricted_sd_after_remount)};
 my($launched,$mounted)=(0,0);my $phase='configuration';my $deadline=$clock->()+300;
 my $check=sub {die 'deadline' if $clock->()>$deadline};
 my $call=sub {my($m,@a)=@_;$check->();if(wantarray){my @v=$g->$m(@a);$check->();return @v}my $v=$g->$m(@a);$check->();return $v};
 my $path='/probe.txt';my $payload="AstinusLab synthetic NTFS creation probe\n";my($ino,$dev);my $sd=descriptor();my $sd_installed=0;
 my $verify_sd=sub {
  my $got=$call->('lgetxattr',$path,'system.ntfs_acl');
  die 'descriptor' unless defined($got)&&!ref($got)&&length($got)==100&&$got eq $sd;
 };
 my $attribute=sub {
  my($path,$directory)=@_;my $a=$call->('lgetxattr',$path,'system.ntfs_attrib_be');
  die 'attribute_length' unless defined($a)&&!ref($a)&&length($a)==4;
  my $flags=unpack('N',$a);die 'attribute_type' if ($flags&0x400)||!!($flags&0x10)!=!!$directory;return $flags;
 };
 my $root_check=sub {
  my %s=$call->('lstatns','/');
  my $mode=uint($s{st_mode},65535);my $d=uint($s{st_dev},9223372036854775807);my $i=uint($s{st_ino},9223372036854775807);
  die 'root_binding' unless S_ISDIR($mode)&&$d==2049&&$i==5;
  die 'root_changed' if defined($dev)&&$dev!=$d;$dev=$d;$attribute->('/',1);
 };
 my $mount_check=sub {
  my($readonly)=@_;my @devices=$call->('list_devices');my @parts=$call->('list_partitions');
  die 'device_binding' unless @devices==1&&$devices[0] eq '/dev/sda'&&@parts==1&&$parts[0] eq '/dev/sda1';
  die 'filesystem' unless $call->('vfs_type','/dev/sda1') eq 'ntfs';
  my @mounts=$call->('mountpoints');die 'mounts' unless @mounts==2&&$mounts[0] eq '/dev/sda1'&&$mounts[1] eq '/';
  my @v=$call->('statvfs','/');die 'shape' unless @v>=2&&@v<=32&&@v%2==0;my %s;
  while(@v){my($k,$v)=splice(@v,0,2);die 'field' unless defined($k)&&!ref($k)&&$k=~/^[a-z_]{1,32}$/&&!exists($s{$k});$s{$k}=$v}
  my $flag=uint($s{flag},9223372036854775807);die 'access' unless $flag>0&&!!($flag&1)==!!$readonly;
  $root_check->();
 };
 my $file_check=sub {
  my($size)=@_;$root_check->();my %s=$call->('lstatns',$path);
  for my $k(qw(st_mode st_dev st_ino st_size st_nlink)){uint($s{$k},$k eq 'st_mode'?65535:9223372036854775807)}
  die 'file_binding' unless S_ISREG($s{st_mode})&&!($s{st_mode}&07000)&&($sd_installed||($s{st_mode}&07777)==0600)&&$s{st_dev}==$dev&&$s{st_nlink}==1&&$s{st_size}==$size&&$s{st_ino}>5;
  die 'inode_changed' if defined($ino)&&$ino!=$s{st_ino};$ino=$s{st_ino};$attribute->($path,0);
  # After descriptor installation, mapped POSIX bits are not the Windows ACL.
  # Exact owner/group/control/ACE bytes become mandatory for every file check.
  $verify_sd->() if $sd_installed;
 };
 my $verify_data=sub {$file_check->(length($payload));die 'payload' unless $call->('pread',$path,length($payload)+1,0) eq $payload;$file_check->(length($payload))};
 eval {
  die 'workspace' unless defined($root)&&!ref($root)&&$root=~m{\A/var/lib/libvirt/images/astinuslab200/ntfs-acl-binary-[0-9]{8}(?:-r[1-9][0-9]{0,2})?\z};
  die 'tool_digest' unless defined($tool_sha)&&!ref($tool_sha)&&$tool_sha=~/\A[0-9a-f]{64}\z/;
  $call->('set_backend','libvirt:qemu:///system');$call->('set_backend_settings',[]);
  $call->('set_network',0);$call->('set_trace',0);$call->('set_verbose',0);
  $call->('set_cachedir',"$root/cache");$call->('set_tmpdir',"$root/tmp");
  $call->('add_drive_opts',"$root/fixture.raw",readonly=>0,format=>'raw');
  $phase='launch';$launched=1;$call->('launch');
  for my $check (RestrictedAcl::capability_checks($tool_sha)) {
   $phase=$check->[0];
   die 'capability' unless $call->('debug','sh',[$check->[1]]) eq $check->[2];
  }
  $phase='format';my @devices=$call->('list_devices');die 'device' unless @devices==1&&$devices[0] eq '/dev/sda';
  die 'capacity' unless $call->('blockdev_getsize64','/dev/sda')==134217728;
  $call->('part_disk','/dev/sda','gpt');die 'gpt' unless $call->('part_get_parttype','/dev/sda') eq 'gpt';
  my @parts=$call->('list_partitions');die 'partition' unless @parts==1&&$parts[0] eq '/dev/sda1';
  $call->('mkfs','ntfs','/dev/sda1');die 'ntfs' unless $call->('vfs_type','/dev/sda1') eq 'ntfs';
  die 'probe' unless $call->('ntfs_3g_probe',1,'/dev/sda1')==0;
  $phase='mount_rw';$mounted=1;$call->('mount_vfs','rw,norecover,posix_nlink,no_def_opts,windows_names,permissions','ntfs-3g','/dev/sda1','/');
  $phase='binding_rw';$mount_check->(0);
  $phase='absence';$check->();my $exists=eval {my %s=$g->lstatns($path);1};my $errno=$exists?0:uint($g->last_errno(),4095);$check->();
  die 'absence' unless !$exists&&$errno==ENOENT;$r->{precreate_binding_verified}=JSON::PP::true;
  $phase='create';$root_check->();$call->('mknod',0100600,0,0,$path);$file_check->(0);
  $phase='duplicate_create';$root_check->();$check->();my $ok=eval {$g->mknod(0100600,0,0,$path);1};$errno=$ok?0:uint($g->last_errno(),4095);$check->();
  die 'exclusive' unless !$ok&&$errno==EEXIST;$file_check->(0);$r->{exclusive_create_verified}=JSON::PP::true;
  $phase='acl_precheck';$file_check->(0);
  $phase='acl_set_rpc';my $reply;
  my $set_ok=eval {$reply=$call->('debug','sh',[RestrictedAcl::setter_command($path)]);1};
  unless($set_ok){$r->{setter_rpc_errno}=uint($g->last_errno(),4095);die 'setter_rpc'}
  $phase='acl_set_returned';$r->{setter_exit}=RestrictedAcl::setter_exit($reply);
  die 'setter_exit' unless $r->{setter_exit}==0;
  $phase='acl_readback';$sd_installed=1;
  $file_check->(0);$r->{restricted_sd_before_write}=JSON::PP::true;
  $phase='write';$file_check->(0);die 'short_write' unless $call->('pwrite',$path,$payload,0)==length($payload);
  $phase='verify';$verify_data->();$r->{payload_verified}=JSON::PP::true;$r->{restricted_sd_after_write}=JSON::PP::true;
  $phase='remount_ro';$call->('sync');$call->('umount_all');$mounted=0;
  my @mounts=$call->('mountpoints');die 'still_mounted' if @mounts;
  die 'probe_ro' unless $call->('ntfs_3g_probe',0,'/dev/sda1')==0;
  $mounted=1;$call->('mount_vfs','ro,norecover,posix_nlink,no_def_opts,windows_names,permissions','ntfs-3g','/dev/sda1','/');
  $phase='binding_ro';$mount_check->(1);
  $phase='verify_ro';$verify_data->();$r->{verification_remount_passed}=JSON::PP::true;$r->{restricted_sd_after_remount}=JSON::PP::true;
  1;
 } or push @fail,$phase;
 my $cleanup=sub {my($label,$method)=@_;my $ok=eval {$g->$method();1};push @fail,$label unless $ok;return $ok};
 my $unmounted=$mounted?$cleanup->('umount','umount_all'):1;my $stopped=$launched?$cleanup->('shutdown','shutdown'):1;my $closed=$cleanup->('close','close');
 $r->{closure_confirmed}=$unmounted&&$stopped&&$closed?JSON::PP::true:JSON::PP::false;
 $r->{appliance_may_be_running}=$launched&&!($stopped&&$closed)?JSON::PP::true:JSON::PP::false;
 $r->{result}='SYNTHETIC_NTFS_ACL_PASS' unless @fail;return $r;
}
1;
