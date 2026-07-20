# Terraform State Adoption and Migration

This runbook prevents Chronos infrastructure from being duplicated, orphaned,
or destroyed while moving to the current Terraform configuration. State work is
a privileged production change because the state contains generated passwords
and sensitive provider inputs.

The current backend is an encrypted, versioned S3 bucket with native S3 lock
files:

```text
bucket: chronos-terraform-state-544294779377-us-east-1
key:    prod/terraform.tfstate
region: us-east-1
lock:   use_lockfile = true
```

That backend is intentionally account-specific. Stop if `aws sts
get-caller-identity` does not return account `544294779377`, or if the intended
environment is not the `prod` state at that key. A different account or
environment needs an explicit reviewed backend configuration; it must not share
this state object.

## Non-negotiable safety rules

- Never run `terraform apply` before state and live-resource inventory agree.
- Never use `terraform state rm`, `terraform import`, `terraform state mv`,
  `-target`, or `-refresh=false` as a trial-and-error debugging tool.
- Never delete or recreate an apparently duplicate resource until ownership,
  data, dependents, and state history are known.
- Never expose `terraform state pull`, a saved plan, or `terraform show -json`
  in CI logs, chat, tickets, or an unencrypted local directory.
- Never force-unlock unless the lock owner and failed operation are identified
  and no Terraform process is still running.
- Keep RDS deletion protection on and S3 `force_destroy=false` throughout
  adoption.
- One operator executes state mutations while a second operator reviews the
  exact address and provider ID.

## 1. Prepare the state backend

From `infra/`, run the idempotent bootstrap with the approved AWS profile:

```bash
bash bootstrap.sh us-east-1 <approved-profile>
```

This creates or revalidates the state bucket, its rotating KMS key, versioning,
public-access block, TLS-only policy, and one-year noncurrent-version retention.
It also ensures the GitHub OIDC provider exists. The bucket and KMS key are
bootstrap dependencies and are not managed inside the same Terraform state.

Initialize without migrating unknown local state:

```bash
terraform init -reconfigure -input=false
terraform providers
```

If Terraform offers to copy local state into S3 and the provenance of that local
file is not documented, answer no and stop for review.

## 2. Capture evidence before mutation

Record:

- AWS account, caller ARN, primary Region, Git commit, and Terraform version;
- current S3 state object version ID and last-modified time;
- `terraform state list` output;
- a tag-based AWS resource inventory for `Application=chronos` and
  `Environment=prod`;
- named global/regional resources that may predate tags, including IAM, ACM,
  Route 53 or external DNS, Cognito, S3, Secrets Manager, ECR, ECS, RDS,
  ElastiCache, ALBs, WAF, Backup, KMS, CloudWatch, SNS, and VPC resources; and
- current DNS answers and GitHub deployment settings.

Save an encrypted local state backup in the restricted change workspace. Do not
print it:

```bash
umask 077
terraform state pull > terraform-state-before-adoption.json
```

Also preserve the S3 state object version ID. S3 version history is the primary
rollback source; the local file is a second, temporary copy.

## 3. Classify every configured address

For every address in `terraform state list`, classify it as exactly one of:

1. **Managed and present:** state ID matches the intended live resource.
2. **Managed but drifted:** resource exists, but mutable attributes differ.
3. **Missing from state:** intended live resource exists and configuration has
   a matching address, but state does not.
4. **Address renamed:** the same live object belongs at a different current
   Terraform address.
5. **State-only:** state references an object that no longer exists.
6. **Unmanaged or foreign:** live resource does not belong to this stack.
7. **Data-bearing conflict:** an intended Terraform name is already occupied by
   a resource whose ownership/data cannot yet be proven.

Resolve categories 6 and 7 before continuing. Terraform must not adopt another
application's resource simply because its name looks familiar.

## 4. Choose the safe adoption action

### Empty state and empty production account

Use the first-deployment bootstrap in
[`PRODUCTION_OPERATIONS.md`](PRODUCTION_OPERATIONS.md). No imports are needed.

### Existing state and matching live resources

Run a refresh-only plan first:

```bash
terraform plan -refresh-only -input=false -out=refresh.plan
terraform show refresh.plan
```

Review every change. Apply a refresh-only plan only when it records observed
drift without changing live infrastructure.

### Intended resource exists but is absent from state

Confirm the exact import ID in the AWS provider documentation, then import one
address at a time:

```bash
terraform import '<current.resource.address>' '<provider-resource-id>'
terraform plan -input=false
```

An import is not complete until the subsequent plan is reviewed. If it proposes
replacement of RDS, S3, KMS, Secrets Manager, Cognito, or an ALB, stop. Change
configuration or use a controlled migration instead of accepting replacement.

### State address changed but the live object is the same

Use an explicit state move:

```bash
terraform state mv '<old.address>' '<current.address>'
terraform plan -input=false
```

Prefer a checked-in Terraform `moved` block for changes that other environments
or future operators may encounter. Use a direct state move only for a one-time
adoption whose old address no longer exists in configuration.

### State references a resource that no longer exists

First prove the provider returns not found and no dependent data or resource
still uses the state identity. Then remove only the stale address:

```bash
terraform state rm '<stale.address>'
terraform plan -input=false
```

`state rm` makes Terraform forget; it does not delete AWS resources. If the
resource still exists, the next apply may create a duplicate or fail on a name
collision.

### Live resource must remain outside this stack

Do not import it. Change the Terraform name/reference or model it as a data
source in a reviewed code change. Document the external owner and lifecycle.

## 5. Transition from DynamoDB locking

The current backend uses native S3 lock files and no longer configures a
DynamoDB lock table. Before decommissioning any legacy lock table:

1. Prove every operator and CI workflow uses the current backend configuration
   and a Terraform version that supports `use_lockfile`.
2. Confirm no active lock exists and no older branch is applying.
3. Run a read-only plan from CI and from the approved operator environment.
4. Retain the legacy table through at least one successful production change
   window.
5. Remove it only through a separately reviewed cleanup change.

Do not delete a DynamoDB table merely because it is absent from current
Terraform configuration.

## 6. First plan after adoption

Copy `terraform.tfvars.example` to an ignored, access-restricted
`terraform.tfvars`, complete the configuration inventory, and run:

```bash
terraform fmt -check -recursive
terraform validate
terraform plan -input=false -out=production.plan
terraform show production.plan
```

The review must confirm:

- the state bucket/key and AWS account are correct;
- `platform_bootstrap_mode` matches the intended deployment phase;
- no data-bearing replacement or deletion is proposed;
- all public domains and ACM ARNs are exact;
- secret values are redacted;
- service counts, database/Redis HA, WAF, alarms, log retention, backups, and
  restore testing satisfy production guards; and
- image tags are immutable outside bootstrap mode.

Store only the encrypted plan artifact for the shortest required review period.
Plan files contain sensitive values.

## 7. Recovering damaged state

Do not immediately push an older state version. First freeze all applies and
identify the last known-good S3 object version. Compare lineage/serial and the
live AWS inventory.

Preferred sequence:

1. Declare an infrastructure incident and disable deploy workflows.
2. Preserve the current state object/version even if it appears corrupt.
3. Download candidate S3 versions into the restricted workspace.
4. Validate JSON, lineage, serial, resource IDs, and provider schemas offline.
5. Have a second operator approve the selected recovery version.
6. Restore through the S3 version mechanism or `terraform state push` only when
   the exact lineage/serial implications are understood.
7. Run `terraform plan -refresh-only`, then a normal plan; do not apply until
   both reconcile with AWS.

State recovery does not recover application data. Use
[`DISASTER_RECOVERY.md`](DISASTER_RECOVERY.md) for database, artifacts,
authorization data, Redis, and secret recovery.

## 8. Adoption completion record

```text
Change/incident ID:
AWS account and caller ARN:
Git commit and Terraform version:
State bucket/key/version before:
Encrypted backup location and deletion date:
Imported addresses and provider IDs:
Moved addresses:
Removed stale addresses and evidence:
Foreign resources explicitly excluded:
Refresh-only plan result:
Final plan result:
Reviewer/operator:
Residual risks:
```

State adoption is complete only when a normal plan is understood line by line;
"no error" is not sufficient evidence.
