# Matzpen CI/CD

Every pull request to `main` runs formatting, lint, type checking, and the complete test suite.
Every push to `main` runs the same checks and deploys only after all of them pass.

The production deployment:

1. creates an immutable archive for the tested commit;
2. uploads it over SSH;
3. locks deployment so two releases cannot overlap;
4. compiles the release and saves an on-server backup;
5. installs Python dependencies and runs Alembic migrations;
6. synchronizes source while preserving `.env`, databases, and the server-specific Gunicorn config;
7. fully restarts the systemd-managed Gunicorn service with exactly one worker, avoiding overlapping Telegram pollers;
8. verifies `/health/live`, `/health/ready`, and the worker count;
9. restores the code backup and reloads the previous version if a step fails.

## GitHub production secrets

Create a GitHub environment named `production` and add these environment secrets:

- `DEPLOY_HOST`: the server hostname or IP address.
- `DEPLOY_PORT`: the SSH port (usually `22`).
- `DEPLOY_USER`: a deployment account allowed to run the deployment script with `sudo`.
- `DEPLOY_SSH_KEY`: its private Ed25519 key, including the header and footer.
- `DEPLOY_KNOWN_HOSTS`: the pinned server host-key line produced by `ssh-keyscan -p PORT HOST` and verified against the server's own host-key fingerprint.

Set the environment variable `CD_ENABLED` to `true` only after all five secrets and the server service are ready. Until then, pushes still run the complete CI job and safely skip production deployment.

The deploy account needs passwordless `sudo` only for:

```text
/usr/bin/bash /tmp/deploy_remote.sh /tmp/matzpen-*.tar.gz <40-character-commit>
```

The server keeps protected backups under `/www/backup/Mazpen`. Deployment fails closed when paths,
the archive name, the Git revision, Gunicorn state, or health checks do not match expectations.

## First activation

Before enabling automatic production deployment:

1. add the SSH public key to the deploy account;
2. copy the tracked files to the server once and run `sudo bash scripts/bootstrap_production_service.sh`;
3. pin and verify the server host key;
4. add the five GitHub production secrets;
5. protect the `production` environment if manual approval is desired;
6. run the workflow once with `workflow_dispatch` and verify the production health endpoints.
