# Official references

Reviewed on 2026-10-28. Runtime behavior is defined by this repository's code/configuration and the current API response; platform documentation can change.

## X

- [Automation rules](https://help.x.com/en/rules-and-policies/x-automation): automated likes, initiated/opted-in replies, one acknowledgement per interaction, opt-outs, and approval rules for certain reply uses.
- [Manage posts](https://docs.x.com/x-api/posts/manage-tweets/introduction): reply creation and current access restrictions.
- [Create posts reference](https://docs.x.com/x-api/posts/create-post): the post/reply write operation.
- [Mentions timeline](https://docs.x.com/x-api/users/get-mentions): incoming interaction discovery and pagination.
- [OAuth 1.0a overview](https://docs.x.com/fundamentals/authentication/oauth-1-0a/overview): app credentials and own-account access tokens.
- [Apps](https://docs.x.com/fundamentals/developer-apps): Read and write permissions, token reauthorization, and callbacks.
- [Developer console](https://docs.x.com/fundamentals/developer-portal): app management, credentials, and billing.
- [Pricing](https://docs.x.com/x-api/getting-started/pricing): credit charging and current endpoint/resource prices.
- [Rate limits](https://docs.x.com/fundamentals/rate-limits): rate-limit responses and reset headers.
- [Official OpenAPI schema](https://docs.x.com/openapi.json): endpoint contracts and supported authentication alternatives.

## GitHub

- [Repository secrets](https://docs.github.com/en/actions/how-tos/write-workflows/choose-what-workflows-do/use-secrets): exact UI for adding credentials.
- [Repository variables](https://docs.github.com/en/actions/how-tos/write-workflows/choose-what-workflows-do/use-variables): non-secret configuration and enable switches.
- [Scheduled workflows](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule): UTC/default-branch behavior, possible delays, and public-repository inactivity disabling.
- [Concurrency](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/control-workflow-concurrency): shared run groups, cancellation, and pending queue behavior.
- [Repository contents API](https://docs.github.com/en/rest/repos/contents#create-or-update-file-contents): durable state writes.

### Verified action pins

| Action | Release | Pinned commit |
| --- | --- | --- |
| `actions/checkout` | [v7.0.1](https://github.com/actions/checkout/releases/tag/v7.0.1) | [`3d3c42e5aac5ba805825da76410c181273ba90b1`](https://github.com/actions/checkout/commit/3d3c42e5aac5ba805825da76410c181273ba90b1) |
| `actions/setup-python` | [v7.1.0](https://github.com/actions/setup-python/releases/tag/v7.1.0) | [`6d4fd22b20465ccc0f7354476588e8df7c9ca0e0`](https://github.com/actions/setup-python/commit/6d4fd22b20465ccc0f7354476588e8df7c9ca0e0) |

Each SHA was checked by following the commit link from that action's official GitHub release page. The workflows use GitHub-hosted `ubuntu-latest` runners.

## Google, for optional alerts

- [Google app passwords](https://support.google.com/accounts/answer/185833): two-step verification prerequisite, account eligibility, generation, and revocation after a Google account password change. Existing working Gmail alert credentials can be reused without creating a new app password.
