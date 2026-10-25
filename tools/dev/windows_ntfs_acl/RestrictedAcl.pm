package RestrictedAcl;
use strict;
use warnings;

# Preparation-only building blocks. The caller must establish an isolated,
# networkless appliance, exclusive filesystem ownership, verified ancestry and
# an empty regular single-link destination before invoking the setter. This
# module neither opens a guestfs handle nor authorizes a candidate write.
sub descriptor {
    my $system = pack('CC C6 V', 1, 1, 0, 0, 0, 0, 0, 5, 18);
    my $admins = pack('CC C6 V2', 1, 2, 0, 0, 0, 0, 0, 5, 32, 544);
    my $aces = pack('CCvV', 0, 0, 20, 0x1f01ff) . $system
             . pack('CCvV', 0, 0, 24, 0x1f01ff) . $admins;
    # SELF_RELATIVE | DACL_PROTECTED | DACL_PRESENT; no SACL/inherited ACEs.
    return pack('CCvVVVV', 1, 0, 0x9004, 72, 84, 0, 20)
         . pack('CCvvv', 2, 0, 52, 2, 0) . $aces . $system . $admins;
}

sub capability_checks {
    my ($sha) = @_;
    die 'tool_digest' unless defined($sha) && !ref($sha) && $sha =~ /\A[0-9a-f]{64}\z/;
    # debug sh executes the appliance /bin/sh, without chrooting into Windows.
    # Its root variable is supplied by guestfsd. No guest executable is used.
    # commandrvf folds stdout onto stderr and removes ALL trailing LF bytes.
    # Expected replies therefore have no final LF; do not normalize other bytes.
    return (
        ['capability_root', 'test "$root" = /sysroot && printf ASTINUS_ROOT_OK', 'ASTINUS_ROOT_OK'],
        ['capability_tools', 'test -x /usr/bin/setfattr && test -x /usr/bin/sha256sum && printf ASTINUS_TOOLS_OK', 'ASTINUS_TOOLS_OK'],
        ['capability_version', 'LC_ALL=C /usr/bin/setfattr --version', 'setfattr 2.5.2'],
        ['capability_digest', '/usr/bin/sha256sum /usr/bin/setfattr', "$sha  /usr/bin/setfattr"],
    );
}

sub setter_command {
    my ($path) = @_;
    die 'path' unless defined($path) && !ref($path) && length($path) <= 260
        && $path =~ m{\A/(?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+\z}
        && !grep { $_ eq '.' || $_ eq '..' } split('/', $path);
    # debug's arguments are XDR strings, then joined with spaces by guestfsd.
    # Send ONE ASCII command; hex has no NUL or shell metacharacters. setfattr
    # decodes the complete value inside the appliance and supplies its length.
    # Do not replace this with guestfs lsetxattr (String + separate Int length).
    return 'LC_ALL=C /usr/bin/setfattr --no-dereference --name=system.ntfs_acl'
         . ' --value=0x' . unpack('H*', descriptor()) . ' -- /sysroot' . $path
         . ' >/dev/null 2>&1; rc=$?; printf \'ASTINUS_ACL_EXIT=%s\' "$rc"';
}

sub setter_exit {
    my ($reply) = @_;
    die 'setter_reply' unless defined($reply) && !ref($reply)
        && $reply =~ /\AASTINUS_ACL_EXIT=(0|[1-9][0-9]{0,2})\z/ && $1 <= 255;
    # Process exit status only, NOT an NTFS syscall errno. RPC failure is a
    # separate caller phase and may expose a bounded guestfs last_errno.
    return 0 + $1;
}

1;
