# Security policy

This project runs commands on a computer on your home network, so security reports are welcome and taken seriously.

**Please report vulnerabilities privately**, using GitHub's private vulnerability reporting on this repository (the *Security* tab, *Report a vulnerability*). Do not open a public issue for a security problem.
Include what you found, how to reproduce it, and what it lets an attacker do. You will get an acknowledgement and an honest assessment; fixes are released as a normal commit and described in the commit message.

What counts: anything that lets a job, a job input, a package, a pack, a repository or a result do something the [security model](docs/components/security.md) says it cannot (run an unlisted command,
reach the network, leak what the guard should catch, gain privileges, forge an approval, install unreviewed code), and any way the wizard or the update tool could leave a machine less safe than it says.
What does not: the known limits listed in that document, and attacks that need your administrator account or laptop to be compromised already.

Supported: the latest commit on the default branch (there are no releases yet).
