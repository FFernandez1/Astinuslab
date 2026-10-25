use strict;use warnings;use Test::More;use JSON::PP ();use Errno qw(EEXIST ENOENT);use SyntheticAclProbe;
{
package Fake;
our $AUTOLOAD;
sub new {bless {events=>[],fault=>$_[1]//'',data=>'',created=>0,mounts=>0,errno=>0},$_[0]}
sub AUTOLOAD {
 my($s,@a)=@_;(my $m=$AUTOLOAD)=~s/.*:://;return if $m eq 'DESTROY';push @{$s->{events}},[$m,@a];
 die 'not logged' if $s->{fault} eq $m;
 return '/dev/sda' if $m eq 'list_devices';return 134217728 if $m eq 'blockdev_getsize64';
 return '/dev/sda1' if $m eq 'list_partitions';return 'gpt' if $m eq 'part_get_parttype';return 'ntfs' if $m eq 'vfs_type';return 0 if $m eq 'ntfs_3g_probe';
 if($m eq 'mount_vfs'){$s->{mounts}++;$s->{mounted}=1;return}
 if($m eq 'umount_all'){$s->{mounted}=0;return}
 if($m eq 'mountpoints'){return () unless $s->{mounted};return $s->{fault} eq 'wrong_mount'?('/dev/sdb'=>'/'):('/dev/sda1'=>'/')}
 if($m eq 'statvfs'){
  return (flag=>undef) if $s->{fault} eq 'undefined_flag';return (flag=>-1) if $s->{fault} eq 'negative_flag';
  return (flag=>1,flag=>1) if $s->{fault} eq 'duplicate_flag';return ({flag=>1}) if $s->{fault} eq 'hashref';
  return (flag=>0) if $s->{fault} eq 'zero_flag';
  return (flag=>($s->{mounts}==1?4097:4096)) if $s->{fault} eq 'wrong_access';
  return (flag=>($s->{mounts}==1?4096:4097));
 }
 if($m eq 'lstatns'){
  if($a[0] eq '/'){
   return (st_mode=>0040777,st_ino=>($s->{fault} eq 'wrong_root_inode'?42:5),st_dev=>($s->{fault} eq 'wrong_root_device'?19:2049),st_size=>4096,st_nlink=>2);
  }
  if(!$s->{created} && $s->{fault} ne 'preexisting'){$s->{errno}=$s->{fault} eq 'absence_denied'?13:Errno::ENOENT();die 'absent'}
  if($s->{mounts}==2 && $s->{fault} eq 'lost_after_remount'){$s->{errno}=Errno::ENOENT();die 'absent'}
  $s->{file_checks}++;
  die 'changed before setter' if $s->{fault} eq 'acl_precheck' && $s->{file_checks}==3;
  return (st_mode=>($s->{sd}?($s->{fault} eq 'special_bits'?0104770:0100770):0100600),st_ino=>($s->{mounts}==2 && $s->{fault} eq 'changed_inode'?65:64),st_dev=>($s->{fault} eq 'wrong_file_device'?19:2049),st_nlink=>($s->{fault} eq 'links'?2:1),st_size=>length($s->{data}));
 }
 if($m eq 'lgetxattr'){
  if($a[1] eq 'system.ntfs_acl'){
   $s->{sd_reads}++;
   return 'invalid' if $s->{fault} eq 'sd_prewrite' && $s->{sd_reads}>1;
   return '' if $s->{fault} eq 'sd_empty';return 'x'x101 if $s->{fault} eq 'sd_oversize';
   return substr($s->{sd},0,2)."\0\0".substr($s->{sd},4) if $s->{fault} eq 'sd_control_changed';
   return 'invalid' if $s->{fault} eq 'sd_mismatch' || ($s->{fault} eq 'sd_after_write' && length($s->{data})) || ($s->{fault} eq 'sd_remount' && $s->{mounts}==2);
   return $s->{sd};
  }
  return 'bad' if $s->{fault} eq 'short_attribute';
  return pack('N',0x400) if $s->{fault} eq 'reparse';
  return pack('N',$a[0] eq '/'?54:32);
 }
 if($m eq 'debug'){
  die 'debug shape' unless $a[0] eq 'sh' && @{$a[1]}==1;
  my %checks=(
   'test "$root" = /sysroot && printf ASTINUS_ROOT_OK'=>['capability_root','ASTINUS_ROOT_OK'],
   'test -x /usr/bin/setfattr && test -x /usr/bin/sha256sum && printf ASTINUS_TOOLS_OK'=>['capability_tools','ASTINUS_TOOLS_OK'],
   'LC_ALL=C /usr/bin/setfattr --version'=>['capability_version','setfattr 2.5.2'],
   '/usr/bin/sha256sum /usr/bin/setfattr'=>['capability_digest',('a'x64).'  /usr/bin/setfattr'],
  );
  if(my $row=$checks{$a[1][0]}) {
   die 'sensitive capability error' if $s->{fault} eq $row->[0].'_rpc';
   return 'mismatch' if $s->{fault} eq $row->[0];
   return $row->[1]; # guestfs debug sh strips trailing LF bytes
  }
  die 'sensitive transport failure' if $s->{fault} eq 'setter_rpc';
  return "ASTINUS_ACL_EXIT=1" if $s->{fault} eq 'setter_exit';
  return 'sensitive malformed reply' if $s->{fault} eq 'setter_reply';
  die 'unexpected command' unless $a[0] eq 'sh' && @{$a[1]}==1 && $a[1][0] eq RestrictedAcl::setter_command('/probe.txt');
  $s->{sd}=RestrictedAcl::descriptor();return "ASTINUS_ACL_EXIT=0";
 }
 if($m eq 'mknod'){
  if($s->{created}){return if $s->{fault} eq 'duplicate_succeeds';$s->{errno}=$s->{fault} eq 'duplicate_errno'?5:Errno::EEXIST();die 'exists'}
  $s->{created}=1;return;
 }
 return $s->{errno} if $m eq 'last_errno';
 if($m eq 'pwrite'){$s->{data}=$a[1];return length($a[1])-($s->{fault} eq 'short_write'?1:0)}
 return $s->{fault} eq 'wrong_payload'?'different':$s->{data} if $m eq 'pread';
 return;
}
}
my $f=Fake->new();my $r=SyntheticAclProbe::collect($f,'/var/lib/libvirt/images/astinuslab200/ntfs-acl-binary-20260920',sub{1},'a'x64);
is($r->{result},'SYNTHETIC_NTFS_ACL_PASS','ACL and persistence pass despite mapped POSIX mode change');
ok($r->{closure_confirmed},'closed');
my @mounts=grep {$_->[0] eq 'mount_vfs'} @{$f->{events}};
is_deeply(\@mounts,[map {['mount_vfs',$_ . ',norecover,posix_nlink,no_def_opts,windows_names,permissions','ntfs-3g','/dev/sda1','/']} ('rw','ro')],'exact planned mount profiles');
my @drives=grep {$_->[0] eq 'add_drive_opts'} @{$f->{events}};
is_deeply(\@drives,[['add_drive_opts','/var/lib/libvirt/images/astinuslab200/ntfs-acl-binary-20260920/fixture.raw','readonly',0,'format','raw']],'only new synthetic fixture');
ok(!grep($_->[0]=~/^(sh|command|lsetxattr|rm|chmod|chown)$/, @{$f->{events}}),'no guest shell, broken setter, chmod, chown or cleanup');
my @set=grep {$_->[0] eq 'debug' && $_->[2][0] eq RestrictedAcl::setter_command('/probe.txt')} @{$f->{events}};
is(scalar(@set),1,'exactly one descriptor installation');
is_deeply($set[0],['debug','sh',[RestrictedAcl::setter_command('/probe.txt')]],'exact public descriptor and path');
my @names=map {$_->[0]} @{$f->{events}};
my($acl)=grep {$names[$_] eq 'debug' && $f->{events}[$_][2][0] eq RestrictedAcl::setter_command('/probe.txt')} 0..$#names;my($write)=grep {$names[$_] eq 'pwrite'} 0..$#names;
ok(defined($acl)&&defined($write)&&$acl<$write,'ACL installed before any payload');
for my $key(qw(restricted_sd_before_write restricted_sd_after_write restricted_sd_after_remount)){ok($r->{$key},"verified $key")}
for my $fault(qw(wrong_mount statvfs undefined_flag negative_flag duplicate_flag hashref zero_flag wrong_access wrong_root_inode wrong_root_device short_attribute reparse preexisting absence_denied)) {
 my $f=Fake->new($fault);my $r=SyntheticAclProbe::collect($f,'/var/lib/libvirt/images/astinuslab200/ntfs-acl-binary-20260920',sub{1},'a'x64);
 is($r->{result},'STOP',"reject before creation $fault");ok(!grep($_->[0] eq 'mknod',@{$f->{events}}),"no creation $fault");ok($r->{closure_confirmed},'closure');
}
for my $fault(qw(wrong_file_device links duplicate_succeeds duplicate_errno lost_after_remount changed_inode short_write wrong_payload mknod pwrite pread lgetxattr setter_rpc setter_exit setter_reply capability_version capability_digest acl_precheck sd_mismatch sd_prewrite sd_empty sd_oversize sd_control_changed special_bits sd_after_write sd_remount launch mkfs mount_vfs sync umount_all shutdown close)) {
 my $f=Fake->new($fault);my $r=SyntheticAclProbe::collect($f,'/var/lib/libvirt/images/astinuslab200/ntfs-acl-binary-20260920',sub{1},'a'x64);
 is($r->{result},'STOP',"reject $fault");ok(grep($_->[0] eq 'close',@{$f->{events}}),'close independently attempted');
 if($fault=~/^(wrong_file_device|links|duplicate_succeeds|duplicate_errno|lgetxattr|setter_rpc|setter_exit|setter_reply|capability_version|capability_digest|acl_precheck|sd_mismatch|sd_prewrite|sd_empty|sd_oversize|sd_control_changed|special_bits)$/){ok(!grep($_->[0] eq 'pwrite',@{$f->{events}}),'no payload after identity/exclusive/ACL failure')}
}
my $late=Fake->new();my $t=0;my $lr=SyntheticAclProbe::collect($late,'/var/lib/libvirt/images/astinuslab200/ntfs-acl-binary-20260920',sub{$t+=301},'a'x64);
is($lr->{result},'STOP','deadline');ok(!grep($_->[0] eq 'launch',@{$late->{events}}),'no late launch');
for my $row (['acl_precheck','acl_precheck'],['capability_digest','capability_digest'],['setter_rpc','acl_set_rpc'],['setter_exit','acl_set_returned'],['setter_reply','acl_set_returned'],['sd_mismatch','acl_readback'],['capability_version','capability_version']) {
 my $r=SyntheticAclProbe::collect(Fake->new($row->[0]),'/var/lib/libvirt/images/astinuslab200/ntfs-acl-binary-20260920',sub{1},'a'x64);
 is_deeply($r->{failure_phases},[$row->[1]],'failure phase distinct: '.$row->[0]);
}
is($r->{setter_exit},0,'setter exit recorded');
is($r->{setter_rpc_errno},undef,'no invented syscall errno');
for my $digest (undef,'','x'x64,'a'x63) {
 my $bad=Fake->new();my $r=SyntheticAclProbe::collect($bad,'/var/lib/libvirt/images/astinuslab200/ntfs-acl-binary-20260920',sub{1},$digest);
 is_deeply($r->{failure_phases},['configuration'],'requires reviewed tool digest');
 ok(!grep($_->[0] eq 'launch',@{$bad->{events}}),'invalid digest never launches');
}
for my $phase (qw(capability_root capability_tools capability_version capability_digest)) {
 for my $suffix ('','_rpc') {
  my $f=Fake->new($phase.$suffix);my $r=SyntheticAclProbe::collect($f,'/var/lib/libvirt/images/astinuslab200/ntfs-acl-binary-20260920-r2',sub{1},'a'x64);
  is_deeply($r->{failure_phases},[$phase],'specific capability failure '.$phase.$suffix);
  ok(!grep($_->[0]=~/^(part_disk|mkfs|mknod|pwrite)$/, @{$f->{events}}),'no formatting or payload on capability failure');
  ok($r->{closure_confirmed},'closed after capability failure');
 }
}
done_testing();
