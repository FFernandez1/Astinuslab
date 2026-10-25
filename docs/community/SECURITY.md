# Security reporting and support

## Where to report

- **Security vulnerabilities:** report them privately through GitHub private
  vulnerability reporting at <https://github.com/FFernandez1/Astinuslab/security/advisories/new>. Only the
  maintainer can see these reports.
- **Bugs and feature requests:** open an issue at <https://github.com/FFernandez1/Astinuslab/issues>.

Do not put vulnerabilities, credentials, memory captures, disk images, personal
data or private infrastructure details into public issues or pull requests.

## Private vulnerability reports

Include the affected version and commit, the supported host environment, impact,
and minimal reproduction steps using synthetic data. Provide redacted logs only
when needed. Coordinate any additional evidence and public disclosure privately
with the maintainer.

## Scope and maintenance

The intended release is AstinusLab 1.0.0 for Ubuntu 24.04 LTS x86_64 with
Python 3.12 or 3.13. Windows functionality is experimental. The platform assumes
a trusted local host; vulnerable training workloads must stay isolated from
untrusted networks. Read the security model in the [Playground guide](../user/PLAYGROUND.md)
before operating a lab.

Francisco Fernandez owns release and reporting-route decisions. Maintenance,
triage and fixes are on demand and best effort. There is no guaranteed response
time, resolution deadline or maintenance window.

Ordinary support uses the [public issue tracker](https://github.com/FFernandez1/Astinuslab/issues). Describe the
problem with synthetic examples and redact private data. Security reports belong
in the private route described above.
