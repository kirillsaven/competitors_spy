# Post-Merge Deploy Verify Baseline

Merge alone does not finish a production task.

Done means:

1. merge
2. deploy
3. verify

Every merged PR that affects runtime or production operations must complete this exact chain:

1. update `origin/main` on the server
2. deploy the current `main`
3. restart the required services
4. confirm the live deployed SHA
5. run one smoke check
6. return a truth report

## Required post-merge checklist

The agent must return all of these facts after merge:

- `live deployed SHA before`
- `live deployed SHA after`
- `was production behind main`
- `services restarted`
- `smoke check report_id` or equivalent smoke signal
- `key symptom fixed / not fixed`

## Helper

Use [scripts/post_merge_deploy_verify.ps1](../scripts/post_merge_deploy_verify.ps1) from the repo root. It reuses the existing production scripts instead of replacing them:

- [scripts/prod-update.sh](../scripts/prod-update.sh)
- [scripts/prod-health.sh](../scripts/prod-health.sh)

Example:

```powershell
pwsh ./scripts/post_merge_deploy_verify.ps1 `
  -Server deploy@YOUR_SERVER_IP `
  -RemoteDir YOUR_APP_DIR `
  -SmokeUserId YOUR_TELEGRAM_USER_ID
```

The helper prints:

- local `origin/main` SHA
- remote deployed SHA before and after deploy
- whether they match
- service status after deploy
- runtime import verification for `tracking/services/reporting.py`
- one fresh smoke report id plus a short rendered preview

If merge happened but deploy and verify did not happen, the PR is not complete.
