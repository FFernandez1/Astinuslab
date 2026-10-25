use strict;use warnings;use Test::More;use JSON::PP ();use CandidateInspection;use RestrictedAcl;
{
 package CandidateFake;
 our $AUTOLOAD;
 sub new {bless {fault=>$_[1]//' ',events=>[],pass=>0},$_[0]}
 sub AUTOLOAD {
  my($s,@a)=@_;(my $m=$AUTOLOAD)=~s/.*:://;return if $m eq 'DESTROY';push @{$s->{events}},[$m,@a];
  die 'private exception' if $s->{fault} eq $m;
  return '/dev/sda' if $m eq 'list_devices';
  return $s->{fault} eq 'capacity'?1:($a[0] eq '/dev/sda'?42949672960:41875931136) if $m eq 'blockdev_getsize64';
  return $s->{fault} eq 'parts'?('/dev/sda1'):('/dev/sda1','/dev/sda2','/dev/sda3') if $m eq 'list_partitions';
  return 'gpt' if $m eq 'part_get_parttype';return $s->{fault} eq 'filesystem'?'BitLocker':'ntfs' if $m eq 'vfs_type';
  return 0 if $m eq 'ntfs_3g_probe';
  if($m eq 'mount_vfs'){$s->{mounted}=1;return}
  if($m eq 'umount_all'){$s->{mounted}=0;return}
  return $s->{mounted}?('/dev/sda3'=>'/'):() if $m eq 'mountpoints';
  if($m eq 'statvfs'){
   my %v=map {$_=>0} qw(bsize frsize blocks bfree bavail files ffree favail fsid flag namemax);
   $v{flag}=$s->{fault} eq 'readonly'?4096:4097;return %v;
  }
  if($m eq 'ls'){
   return ('Windows',($s->{fault} eq 'collision'?'WINDOWS':())) if $a[0] eq '/';
   return $s->{fault} eq 'missing_parent'?():('Panther') if $a[0] eq '/Windows';
   return $s->{fault} eq 'case_destination'?('UnAttEnD.XmL'):();
  }
  if($m eq 'lstatns'){
   if($a[0]=~/unattend\.xml$/){$s->{errno}=$s->{fault} eq 'absence_denied'?13:2;die 'absent'}
   $s->{pass}++ if $a[0] eq '/';
   # Model the complete installed StatNS struct, including numbered spare keys.
   my %v=map {$_=>0} qw(st_dev st_ino st_mode st_nlink st_uid st_gid st_rdev st_size st_blksize st_blocks st_atime_sec st_atime_nsec st_mtime_sec st_mtime_nsec st_ctime_sec st_ctime_nsec st_spare1 st_spare2 st_spare3 st_spare4 st_spare5 st_spare6);
   @v{qw(st_dev st_ino st_mode st_nlink st_size)}=($s->{fault} eq 'device'?2049:2051,$a[0] eq '/'?5:$a[0] eq '/Windows'?30:40,$s->{fault} eq 'symlink'?0120777:0040777,2,4096);
   if($s->{fault} eq 'unknown_field'){delete $v{st_spare6};$v{st_spare7}=0}
   delete $v{st_spare6} if $s->{fault} eq 'missing_field'||$s->{fault} eq 'duplicate_field';
   return (%v,st_dev=>2051) if $s->{fault} eq 'duplicate_field';return %v;
  }
  return $s->{errno} if $m eq 'last_errno';
  if($m eq 'lgetxattr'){
   return pack('N',$s->{fault} eq 'reparse'?0x410:0x10) if $a[1] eq 'system.ntfs_attrib_be';
   return 'x'x8193 if $s->{fault} eq 'acl_bound';
   return RestrictedAcl::descriptor().($s->{fault} eq 'changed'&&$s->{pass}>1?'x':'');
  }
  die 'unexpected mutation' if $m=~/^(mknod|pwrite|write|debug|lsetxattr|mkfs|part_disk|sync)$/;
  return;
 }
}
my $root='/var/lib/libvirt/images/astinuslab200/unattended-b-audit-20260920';
my $f=CandidateFake->new('');my $r=CandidateInspection::collect($f,$root,sub{1});
is($r->{result},'CANDIDATE_METADATA_RECORDED','read-only metadata collected');
ok($r->{closure_confirmed}&&!$r->{appliance_may_be_running},'closed');
ok($r->{destination_absent}&&$r->{repeat_matches},'absence and repeated metadata');
is(scalar @{$r->{directories}},3,'only three ancestors');
is_deeply([map {$_->{path}} @{$r->{directories}}],['/','/Windows','/Windows/Panther'],'exact paths');
my @drives=grep {$_->[0] eq 'add_drive_opts'} @{$f->{events}};
is_deeply(\@drives,[['add_drive_opts',"$root/inspection.qcow2",readonly=>1,format=>'qcow2']],'only isolated readonly copy');
my @mounts=grep {$_->[0] eq 'mount_vfs'} @{$f->{events}};
is_deeply(\@mounts,[['mount_vfs','ro,norecover,posix_nlink,no_def_opts,windows_names,permissions','ntfs-3g','/dev/sda3','/']],'one readonly exact profile');
my %phases=map {$_=>1} CandidateInspection::phases();
my %expected=(unknown_field=>'root_stat',missing_field=>'root_stat',duplicate_field=>'root_stat',device=>'root_binding',symlink=>'root_binding',reparse=>'root_attribute',acl_bound=>'root_acl',collision=>'windows_lookup',missing_parent=>'panther_lookup',case_destination=>'destination_lookup',absence_denied=>'destination_stat',changed=>'repeat_compare');
for my $fault(qw(capacity parts filesystem readonly collision missing_parent case_destination absence_denied device symlink reparse acl_bound changed unknown_field missing_field duplicate_field launch lstatns lgetxattr ls umount_all shutdown close)) {
 my $g=CandidateFake->new($fault);my $v=CandidateInspection::collect($g,$root,sub{1});
 is($v->{result},'STOP',"$fault stops");ok(@{$v->{failure_phases}}>0,"$fault phase");
 ok(!(grep {$_->[0]=~/^(mknod|pwrite|debug|lsetxattr|mkfs|part_disk)$/} @{$g->{events}}),"$fault no mutation");
 ok((grep {$_->[0] eq 'close'} @{$g->{events}}),"$fault close attempted");
 ok(!(grep {!$phases{$_}} @{$v->{failure_phases}}),"$fault bounded phases");
 is($v->{failure_phases}[0],$expected{$fault},"$fault precise phase") if exists $expected{$fault};
 if($fault eq 'missing_parent'){is(scalar @{$v->{directories}},2,'completed ancestors survive later failure')}
 if($fault eq 'device'){is($v->{last_stat}{st_dev},2049,'wrong device retained as bounded evidence')}
}
my $bad=CandidateFake->new('');my $v=CandidateInspection::collect($bad,'/var/lib/libvirt/images/astinuslab200/wintrial-2022-20260913-unattended-b',sub{1});
is($v->{failure_phases}[0],'configuration','existing candidate cannot be attached');
ok(!(grep {$_->[0] eq 'add_drive_opts'} @{$bad->{events}}),'no attachment for invalid workspace');
done_testing();
