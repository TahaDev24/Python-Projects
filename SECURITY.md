# Security Policy

The projects in this repository may connect to messaging platforms and store local application data. Protecting credentials and user data is important.

## Reporting a vulnerability

Do not publish exploitable details, active tokens, personal information, or private database files in a public issue.

For a private report, use the private contact method available on the maintainer's GitHub profile:

- [@TahaDev24](https://github.com/TahaDev24)

Include a concise description, affected project, potential impact, and reproduction details that do not expose real credentials or user data.

## Sensitive information

Never commit or publicly share:

- Bale bot tokens or API credentials.
- Passwords and authentication data.
- Private user or group identifiers when they are not intended for publication.
- Runtime databases, backups, or logs containing real user activity.
- Environment files containing secrets.

## If a credential is exposed

1. Revoke the exposed token or credential through the relevant service.
2. Create a replacement credential.
3. Update the local deployment configuration.
4. Review repository history and any logs or copies where the credential may remain.
5. Do not assume that deleting a secret from the latest file removes it from Git history.

## Supported versions

This repository contains independently maintained projects and does not currently publish a formal support matrix. When reporting a security issue, include the project name, the commit or version you are using, and your runtime environment.
