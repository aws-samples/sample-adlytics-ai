---
inclusion: always
description: Governs data, credential and security-control safety on every turn in an Adlytics AI checkout; complements the Git, identity and evidence rules other steering owns.
---

# Safety in a customer checkout

- Run validation probes and acceptance runners only against an environment named dev that holds
  synthetic data. Never point one at an environment holding customer data.
- Never commit a filled copy of validation/env.example, a session cookie, or raw probe output;
  they hold credentials or exact live identifiers. validation-probes owns where probe evidence
  is kept.
- Never weaken an authentication check, a WAF rule, a deletion protection or an IAM boundary to
  make a deploy, test or probe pass. Stop and ask the user, naming the control and the risk.
- Never record a CRITICAL or HIGH security finding as ACCEPTED. docs/security-dispositions.md owns
  the dispositions and their meaning.
