use strict;use warnings;use Test::More;use JSON::PP ();use Digest::SHA qw(sha256_hex);
use RestrictedAcl;use CandidateInspection;use CandidatePlacement;
{
 package PlacementFake;
 our $AUTOLOAD;
 sub baseline {
  my $sd=RestrictedAcl::descriptor();
  return [map {{path=>$_->[0],st_dev=>2051,st_ino=>$_->[1],st_mode=>0040755,st_nlink=>2,st_size=>4096,
   ntfs_flags=>16,sd_hex=>unpack('H*',$sd),sd_sha256=>Digest::SHA::sha256_hex($sd)}} (['/',5],['/Windows',536],['/Windows/Panther',45])];
 }
 sub new {bless {fault=>$_[1]//'',events=>[],expected=>baseline(),created=>0,data=>'',mounts=>0,errno=>0},$_[0]}
 sub AUTOLOAD {
  my($s,@a)=@_;(my $m=$AUTOLOAD)=~s/.*:://;return if $m eq 'DESTROY';push @{$s->{events}},[$m,@a];
  die 'private RPC failure' if $s->{fault} eq $m;
  return '/dev/sda' if $m eq 'list_devices';return ('/dev/sda1','/dev/sda2','/dev/sda3') if $m eq 'list_partitions';
  return $a[0] eq '/dev/sda'?42949672960:41875931136 if $m eq 'blockdev_getsize64';
  return 'gpt' if $m eq 'part_get_parttype';return 'ntfs' if $m eq 'vfs_type';return 0 if $m eq 'ntfs_3g_probe';
  if($m eq 'mount_vfs'){$s->{mounts}++;$s->{mounted}=1;return}
  if($m eq 'umount_all'){$s->{mounted}=0;return}
  return $s->{mounted}?('/dev/sda3'=>'/'):() if $m eq 'mountpoints';
  if($m eq 'statvfs'){my %v=map {$_=>0} CandidateInspection::statvfs_fields();$v{flag}=$s->{mounts}==1?4096:4097;return %v}
  if($m eq 'ls'){
   return $s->{fault} eq 'case_collision'?('Windows','WINDOWS'):('Windows') if $a[0] eq '/';
   return ('Panther') if $a[0] eq '/Windows';
   return ($s->{created}||$s->{fault} eq 'preexisting')?('unattend.xml'):();
  }
  if($m eq 'lstatns'){
   my %v=map {$_=>0} CandidateInspection::statns_fields();
   my($e)=grep {$_->{path} eq $a[0]} @{$s->{expected}};
   if($e){@v{qw(st_dev st_ino st_mode st_nlink st_size)}=@$e{qw(st_dev st_ino st_mode st_nlink st_size)};
    $v{st_ino}++ if $s->{fault} eq 'ancestor_changed'&&$s->{created};return %v;}
   if(!$s->{created}){$s->{errno}=$s->{fault} eq 'absence_denied'?13:2;die 'absent'}
   @v{qw(st_dev st_ino st_mode st_nlink st_size)}=(2051,1000,$s->{sd}?0100770:0100600,1,length($s->{data}));
   $v{st_nlink}=2 if $s->{fault} eq 'hardlink';$v{st_ino}++ if $s->{fault} eq 'remount_inode'&&$s->{mounts}==2;return %v;
  }
  return $s->{errno} if $m eq 'last_errno';
  if($m eq 'lgetxattr'){
   my($e)=grep {$_->{path} eq $a[0]} @{$s->{expected}};
   if($a[1] eq 'system.ntfs_attrib_be'){return pack('N',$s->{fault} eq 'reparse'?0x410:($e?16:32))}
   return 'changed' if $e&&$s->{fault} eq 'ancestor_acl';
   return pack('H*',$e->{sd_hex}) if $e;
   return 'changed' if $s->{fault} eq 'sd_prewrite'||($s->{fault} eq 'sd_after_write'&&length($s->{data}))||($s->{fault} eq 'sd_remount'&&$s->{mounts}==2);
   return $s->{sd};
  }
  if($m eq 'mknod'){
   if($s->{created}){return if $s->{fault} eq 'duplicate_succeeds';$s->{errno}=17;die 'exists'}
   $s->{created}=1;return;
  }
  if($m eq 'debug'){
   for my $check(RestrictedAcl::capability_checks('a'x64)){return $s->{fault} eq 'capability'?'wrong':$check->[2] if $a[1][0] eq $check->[1]}
   die 'setter private error' if $s->{fault} eq 'setter_rpc';
   return 'ASTINUS_ACL_EXIT=1' if $s->{fault} eq 'setter_exit';
   die 'unexpected command' unless $a[1][0] eq RestrictedAcl::setter_command('/Windows/Panther/unattend.xml');
   $s->{sd}=RestrictedAcl::descriptor();return 'ASTINUS_ACL_EXIT=0';
  }
  if($m eq 'pwrite'){
   die 'ACL missing before payload' unless defined($s->{sd})&&$s->{sd} eq RestrictedAcl::descriptor();
   $s->{data}=$a[1];return length($a[1])-($s->{fault} eq 'short_write'?1:0);
  }
  return $s->{fault} eq 'wrong_payload'?'wrong':$s->{data} if $m eq 'pread';
  die 'forbidden API' if $m=~/^(lsetxattr|mkfs|part_disk|chmod|chown|rm|upload)$/;
  return;
 }
}
my $work='/var/lib/libvirt/images/astinuslab200/unattended-b-placement-20260920';
my $xml='<?xml version="1.0"?><unattend>public fixture only</unattend>';
my $f=PlacementFake->new();my $r=CandidatePlacement::collect($f,$work,$xml,PlacementFake::baseline(),'a'x64,sub{1});
is($r->{result},'CANDIDATE_UNATTEND_PLACED','secure placement completes');
for my $k(qw(ancestry_verified destination_absent exclusive_create_verified restricted_sd_before_write restricted_sd_after_write restricted_sd_after_remount payload_verified verification_remount_passed closure_confirmed)){ok($r->{$k},$k)}
my @drives=grep {$_->[0] eq 'add_drive_opts'} @{$f->{events}};
is_deeply(\@drives,[['add_drive_opts','/var/lib/libvirt/images/astinuslab200/wintrial-2022-20260913-unattended-b/disk.qcow2',readonly=>0,format=>'qcow2']],'only approved B disk');
my %before_payload=map {$_=>1} qw(capability case_collision preexisting absence_denied ancestor_acl ancestor_changed reparse hardlink duplicate_succeeds setter_rpc setter_exit sd_prewrite);
for my $fault(qw(capability case_collision preexisting absence_denied ancestor_acl ancestor_changed reparse hardlink duplicate_succeeds setter_rpc setter_exit sd_prewrite sd_after_write sd_remount remount_inode short_write wrong_payload launch shutdown close)) {
 my $g=PlacementFake->new($fault);my $v=CandidatePlacement::collect($g,$work,$xml,PlacementFake::baseline(),'a'x64,sub{1});
 is($v->{result},'STOP',"$fault stops");ok(@{$v->{failure_phases}},"$fault phase");
 ok(!(grep {$_->[0] eq 'pwrite'} @{$g->{events}}),"$fault no payload") if $before_payload{$fault};
 ok((grep {$_->[0] eq 'close'} @{$g->{events}}),"$fault close attempted");
}
my $bad=PlacementFake->new();$r=CandidatePlacement::collect($bad,$work,'',PlacementFake::baseline(),'a'x64,sub{1});
is($r->{failure_phases}[0],'configuration','empty payload rejected before attachment');
ok(!(grep {$_->[0] eq 'add_drive_opts'} @{$bad->{events}}),'no attachment');
done_testing();
