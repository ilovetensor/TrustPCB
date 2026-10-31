# Security policy

## Reporting a vulnerability

Please report security problems privately, not in a public issue or pull request.

Use GitHub's private vulnerability reporting: open the **Security** tab of this repository and choose **Report a vulnerability**. That opens a private advisory that only you and the maintainers can see. Include what you found, how to reproduce it, and the version (from `package.json`) or commit you tested.

The maintainers will acknowledge a report, look into it, and tell you what they decide. This is a volunteer project, so replies are not instant. Please give us a reasonable time to fix a problem before you tell anyone else about it.

> Maintainer note: private vulnerability reporting is off by default. Turn it on once the repository exists, under Settings, Code security, Private vulnerability reporting. Until it is on, the **Report a vulnerability** button will not appear.

## What is in scope

TrustPCB is a local tool. The backend binds to `127.0.0.1`, requires a per-launch token and checks the `Host` and `Origin` headers, and it has no accounts. It is not designed to be exposed to the internet, and running it that way is not a supported setup. Reports about hosted use of the app are out of scope; reports about the following are welcome:

- Ways to bypass the token, `Host` or `Origin` checks from another site or process
- Ways for an uploaded board or zip to read or write files outside the data directory (for example path traversal in intake)
- Ways for the agent's tools to do more than they are allowed to (read files, run commands, reach the network)
- Secrets ending up in logs, stored reviews, reports or exports
- TrustPCB changing the user's original files

## Secrets

Never put a key or token in an issue, a pull request or a test. The Muse key is read only from the environment (`MODEL_API_KEY`) and `.env` is git-ignored. If you think a key has leaked, revoke it first and then tell us.
