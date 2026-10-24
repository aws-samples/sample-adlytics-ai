# Instructions for AI coding assistants

Adlytics AI is sample code for retail-media analytics on AWS. Start with [README.md](README.md),
then [docs/README.md](docs/README.md) for the design index. Kiro users get the full rule set from
`.kiro/steering/`; [docs/kiro.md](docs/kiro.md) explains it and lists starter prompts.

The essentials, for any assistant:

- **No invented figures.** Every number the product shows or says comes from a governed tool over
  published data. Never add code that lets a model compute, total or estimate a figure.
- **Contracts first.** Change the declared contract under `contracts/` or `presentation/`, then run
  the generator the README's "Extend it" table names. Never hand-edit a generated file.
- **Prove it locally.** Run the gates [CONTRIBUTING](CONTRIBUTING.md#run-the-gates) lists for the
  surface you changed before saying a change works. There is no remote CI.
- **Explicit AWS target.** Every command that reaches AWS takes `PROFILE=` and `ENV=` with no
  default. Run `make deploy-all` without `APPROVE=1` first, show the user the account, Region and
  diff it prints, and add `APPROVE=1` only after they confirm.
- **Ask first** before destroying an environment, deleting a retained resource, turning off
  deletion protection, or weakening any authentication, WAF or IAM control.
- **Keep secrets out.** Never commit `.env`, session cookies, credentials or real account
  identifiers. Customer data never leaves the customer's AWS account.
- **Probes are dev-only.** Validation probes run only against an environment named `dev` holding
  synthetic data.
