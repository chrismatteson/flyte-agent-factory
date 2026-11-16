# The factory bucket in Terraform

What the factory's own bucket needs from the infrastructure repositories, so that `[state] backend = "bucket"`
([state-store.md](state-store.md)) works on a cluster you built with them. This page is the requirement; the
snippets show the shape and are checked against the repositories' layout, not applied. Read it with
[state-escalation.md](state-escalation.md), which says when this backend stops being enough.

The two repositories, as they stand when this was written:

| Repository | Clouds with Terraform | Where the Flyte bucket and the pod identities are defined |
|---|---|---|
| [`unionai-oss/terraform-flyte`](https://github.com/unionai-oss/terraform-flyte) (OSS Flyte) | `aws/`, `azure/`. **`gcp/` is a placeholder** with a README and no `.tf` yet | `aws/infra/flyte.tf`: `aws_s3_bucket.this` (named `local.unique_name` = `<name>-<random hex>`), `aws_s3_bucket_policy.this` (TLS only), `aws_s3_bucket_public_access_block.this`; `local.service_accounts` (`backend` and `task`, namespace `flyte`) with one IRSA role each, `aws_iam_role.workload["backend"\|"task"]`; outputs `bucket_name`, `backend_role_arn`, `service_accounts` in `aws/infra/outputs.tf` |
| [`unionai-oss/terraform-union-dataplane`](https://github.com/unionai-oss/terraform-union-dataplane) (Union BYOC dataplane) | `aws/`, `azure/`, `gcp/`, `coreweave/`, `crusoe/`, `nebius/`, `oci/` | `aws/infra/dataplane.tf`: `aws_s3_bucket.this["metadata"\|"fast_reg"]`, one role `aws_iam_role.union_system` that trusts `system:serviceaccount:*` of the cluster's OIDC provider; `gcp/infra/dataplane.tf`: `google_storage_bucket.metadata` / `.fast_registration`, `google_service_account.union` with Workload Identity bindings for the `union-system` and `union` Kubernetes service accounts; outputs `bucket_name`, `iam_role_arn` / `service_account_email`, `region` |

Both repositories use two root modules per cloud, applied in order: `<cloud>/infra/` (cloud API: buckets, IAM) and
`<cloud>/helm/` (Kubernetes objects, which reads the infra root's outputs). The additions below belong in
`infra/`, in a new file per repository and cloud (`factory_bucket.tf`), with the variables in `variables.tf` and the
outputs in `outputs.tf`. The two repositories are kept in lockstep (their `AGENTS.md`), so each addition goes into
both. The `helm/` root only needs the dedicated Kubernetes service accounts (below) and the outputs.

## What the bucket is

A bucket **separate from the Flyte data bucket**. The engine refuses to start on the Flyte or Union data bucket
(`refuse_flyte_bucket`), because Flyte's lifecycle and access rules are the opposite of what state needs: Flyte
deletes and rewrites freely and every task role can read it, while state needs versioning, a short audit
retention and access for the factory only.

| Setting | Requirement |
|---|---|
| Name | variable `factory_bucket_name`, default `null`; when null, `<name>-factory-<random hex>` (the repositories' `random_id.suffix`), so two copies run side by side |
| Layout | the engine uses the bucket root: `state/`, `audit/` and `browser/`. Output the URI **without a path prefix** (`s3://bucket`), because the IAM prefixes below are `state/*`, `audit/*`, `browser/*` |
| Versioning | **on**: an overwritten or deleted state object stays recoverable |
| Noncurrent versions | expire after `factory_noncurrent_days` (default 30). The dispatch lease and every counter are overwritten constantly, so without this the bucket grows by one version per write |
| `audit/` | current objects expire after `factory_audit_days` (default 30, the engine's `retention_days`). The engine prunes too; the rule is the backstop |
| Incomplete multipart uploads | abort after 7 days (AWS) |
| Encryption | at rest: AWS SSE-S3 by default, SSE-KMS when `factory_kms_key_arn` is set; GCS Google-managed by default, CMEK when `factory_kms_key_name` is set |
| Public access | blocked: S3 public access block (all four on); GCS `public_access_prevention = "enforced"` and `uniform_bucket_level_access = true` |
| Transport | TLS only: bucket policy deny on `aws:SecureTransport = false` (as `flyte.tf` already does for the Flyte bucket); GCS is TLS-only on the public endpoints |

## Who may do what

| Identity | Allowed | Everything else |
|---|---|---|
| The factory's pods (the slot / factory service account) | read, write, delete on `state/*` and `audit/*`; list (on GCP: the whole bucket's object names) | denied |
| The personal browser app | read, write on `browser/*`. On AWS listing is limited to that prefix; on GCP it cannot be (see [GCP](#gcp)) | denied |
| Flyte's own task and backend roles (`flyte-task`, `flyte-backend`, Union's `union_system`) | nothing | denied by the bucket policy |
| Break-glass operators, the Terraform applier | as `factory_breakglass_arns` says (default none) | |

The engine runs under these accounts: `[state] factory_service_account` is set on every pod that touches the
state (slots, factory, scan, MCP, issues, reconcile, loop) and `browser_service_account` on the personal browser
apps. `ops/bootstrap.py --terraform-dir DIR` reads both from the `factory_service_accounts` output into `[state]`
(`--factory-service-account` / `--browser-service-account` override). Without them the pods run as the
namespace's default account and the policy below denies them.

Two requirements that follow from the repositories' current shape:

1. **A dedicated service account for the factory.** `terraform-flyte` has `flyte-backend` and `flyte-task`;
   granting the bucket to `flyte-task` would give it to every task pod. Add a third entry to
   `local.service_accounts` (`factory = { namespace = "flyte", name = "flyte-factory" }`; for Union a
   `factory_service_account` variable) with its own IRSA role or Google service account, create the Kubernetes service account in
   `helm/flyte.tf` the way `kubernetes_service_account_v1.task` is, and run the factory's slots under it. In
   `terraform-union-dataplane` the one `union_system` role trusts `system:serviceaccount:*`, so it cannot be
   narrowed; the factory gets a second role that trusts only its service account, and the bucket policy's
   principal deny is what keeps `union_system` out.
2. **The browser app has its own identity too**, a fourth service account and role (variable
   `browser_service_account`), so that `browser/*` is reachable by it alone.

`If-Match` writes need `s3:GetObject` as well as `s3:PutObject`
([AWS](https://docs.aws.amazon.com/AmazonS3/latest/userguide/conditional-writes.html)); the policies below have both.

## AWS

Added to `aws/infra/` of both repositories. The bucket:

```hcl
variable "factory_bucket_name" {
  description = "Name of the factory's state bucket. Null: <name>-factory-<suffix>."
  type        = string
  default     = null
}

variable "factory_noncurrent_days" {
  description = "Days a noncurrent version of a state object is kept."
  type        = number
  default     = 30
}

variable "factory_audit_days" {
  description = "Days an audit record is kept."
  type        = number
  default     = 30
}

variable "factory_kms_key_arn" {
  description = "KMS key for SSE-KMS. Null: SSE-S3."
  type        = string
  default     = null
}

variable "factory_breakglass_arns" {
  description = "Principals exempt from the bucket policy's deny (the Terraform applier, an operator role)."
  type        = list(string)
  default     = []
}

locals {
  factory_bucket = coalesce(var.factory_bucket_name, "${var.name}-factory-${random_id.suffix.hex}")
}

resource "aws_s3_bucket" "factory" {
  bucket = local.factory_bucket
}

resource "aws_s3_bucket_public_access_block" "factory" {
  bucket                  = aws_s3_bucket.factory.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "factory" {
  bucket = aws_s3_bucket.factory.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "factory" {
  bucket = aws_s3_bucket.factory.id
  rule {
    bucket_key_enabled = var.factory_kms_key_arn != null
    apply_server_side_encryption_by_default {
      sse_algorithm     = var.factory_kms_key_arn == null ? "AES256" : "aws:kms"
      kms_master_key_id = var.factory_kms_key_arn
    }
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "factory" {
  bucket = aws_s3_bucket.factory.id

  # Versioning must be on first; the lifecycle API rejects a versioned rule otherwise.
  depends_on = [aws_s3_bucket_versioning.factory]

  rule {
    id     = "expire-noncurrent-versions"
    status = "Enabled"
    filter {}
    noncurrent_version_expiration {
      noncurrent_days = var.factory_noncurrent_days
    }
    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }

  rule {
    id     = "expire-audit"
    status = "Enabled"
    filter {
      prefix = "audit/"
    }
    expiration {
      days = var.factory_audit_days
    }
  }

  rule {
    # A deleted state key leaves a delete marker once its versions are gone; it is only clutter in a listing.
    id     = "expire-delete-markers"
    status = "Enabled"
    filter {}
    expiration {
      expired_object_delete_marker = true
    }
  }
}
```

The access policies, one document per identity:

```hcl
data "aws_iam_policy_document" "factory_access" {
  statement {
    sid       = "ListStateAndAudit"
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.factory.arn]
    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values   = ["state/*", "audit/*"]
    }
  }
  statement {
    sid       = "ObjectsUnderStateAndAudit"
    actions   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
    resources = ["${aws_s3_bucket.factory.arn}/state/*", "${aws_s3_bucket.factory.arn}/audit/*"]
  }
  dynamic "statement" {
    for_each = var.factory_kms_key_arn == null ? [] : [1]
    content {
      sid       = "Kms"
      actions   = ["kms:Decrypt", "kms:GenerateDataKey"]
      resources = [var.factory_kms_key_arn]
    }
  }
}

data "aws_iam_policy_document" "browser_access" {
  statement {
    sid       = "ListBrowser"
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.factory.arn]
    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values   = ["browser/*"]
    }
  }
  statement {
    sid       = "ObjectsUnderBrowser"
    actions   = ["s3:GetObject", "s3:PutObject"]
    resources = ["${aws_s3_bucket.factory.arn}/browser/*"]
  }
}
```

The documents grant nothing until they are attached to a role. The bucket policy below names the two roles through
`local.factory_role_arn` and `local.browser_role_arn`; each repository defines them with its own roles.

In `terraform-flyte`, `local.service_accounts` gains `factory` and `browser` entries, so `aws_iam_role.workload` and
its IRSA trust documents already cover them. Attach the documents to those roles:

```hcl
locals {
  factory_role_arn = aws_iam_role.workload["factory"].arn
  browser_role_arn = aws_iam_role.workload["browser"].arn
}

resource "aws_iam_role_policy" "factory_access" {
  name   = "factory-bucket"
  role   = aws_iam_role.workload["factory"].name
  policy = data.aws_iam_policy_document.factory_access.json
}

resource "aws_iam_role_policy" "browser_access" {
  name   = "browser-bucket"
  role   = aws_iam_role.workload["browser"].name
  policy = data.aws_iam_policy_document.browser_access.json
}
```

In `terraform-union-dataplane` there is only `aws_iam_role.union_system`, which trusts every service account of the
cluster and so cannot be narrowed. Add two roles, each trusting one Kubernetes service account through the cluster's
OIDC provider (the same provider `union_system` trusts; use the expressions its trust document already uses if they
differ from `module.eks.*` below). The service account names are variables, as for the GCP section:

```hcl
variable "namespace" {
  description = "Kubernetes namespace of the factory's and the browser app's service accounts."
  type        = string
  default     = "union"
}

variable "factory_service_account" {
  description = "Kubernetes service account the factory's slots run under."
  type        = string
  default     = "flyte-factory"
}

variable "browser_service_account" {
  description = "Kubernetes service account of the personal browser app."
  type        = string
  default     = "factory-browser"
}

locals {
  factory_identities = {
    factory = var.factory_service_account
    browser = var.browser_service_account
  }
}

data "aws_iam_policy_document" "factory_trust" {
  for_each = local.factory_identities

  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [module.eks.oidc_provider_arn]
    }
    condition {
      test     = "StringEquals"
      variable = "${module.eks.oidc_provider}:aud"
      values   = ["sts.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "${module.eks.oidc_provider}:sub"
      values   = ["system:serviceaccount:${var.namespace}:${each.value}"]
    }
  }
}

resource "aws_iam_role" "factory" {
  name               = "${var.name}-factory"
  assume_role_policy = data.aws_iam_policy_document.factory_trust["factory"].json
}

resource "aws_iam_role" "browser" {
  name               = "${var.name}-factory-browser"
  assume_role_policy = data.aws_iam_policy_document.factory_trust["browser"].json
}

locals {
  factory_role_arn = aws_iam_role.factory.arn
  browser_role_arn = aws_iam_role.browser.arn
}

resource "aws_iam_role_policy" "factory_access" {
  name   = "factory-bucket"
  role   = aws_iam_role.factory.name
  policy = data.aws_iam_policy_document.factory_access.json
}

resource "aws_iam_role_policy" "browser_access" {
  name   = "browser-bucket"
  role   = aws_iam_role.browser.name
  policy = data.aws_iam_policy_document.browser_access.json
}
```

Create the two Kubernetes service accounts in `helm/` with the `eks.amazonaws.com/role-arn` annotation set to the
role's ARN (outputs below), the way `kubernetes_service_account_v1.task` is in `terraform-flyte`.

(`s3:ListBucket` with `StringLike s3:prefix` also needs the bare `state` / `audit` / `browser` listing the engine
makes with a delimiter; add `"state", "audit", "browser"` to each `values` list if a list of the prefix itself is
refused when you apply it.)

The bucket policy is the boundary that does not depend on what IAM policies the other roles carry:

```hcl
data "aws_iam_policy_document" "factory_bucket" {
  statement {
    sid       = "DenyInsecureTransport"
    effect    = "Deny"
    actions   = ["s3:*"]
    resources = [aws_s3_bucket.factory.arn, "${aws_s3_bucket.factory.arn}/*"]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }

  # Objects and listings: only the factory, the browser app and the break-glass principals. This keeps
  # flyte-task, flyte-backend and union_system out even though their own policies are broad elsewhere.
  statement {
    sid       = "DenyDataToOtherPrincipals"
    effect    = "Deny"
    actions   = ["s3:GetObject*", "s3:PutObject*", "s3:DeleteObject*", "s3:ListBucket*"]
    resources = [aws_s3_bucket.factory.arn, "${aws_s3_bucket.factory.arn}/*"]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    condition {
      test     = "StringNotEquals"
      variable = "aws:PrincipalArn"
      values = concat(
        [local.factory_role_arn, local.browser_role_arn],
        var.factory_breakglass_arns,
      )
    }
  }

  # browser/* belongs to the browser app alone: not even the factory role, whatever its policy says.
  statement {
    sid       = "DenyBrowserPrefixToOthers"
    effect    = "Deny"
    actions   = ["s3:*"]
    resources = ["${aws_s3_bucket.factory.arn}/browser/*"]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    condition {
      test     = "StringNotEquals"
      variable = "aws:PrincipalArn"
      values   = concat([local.browser_role_arn], var.factory_breakglass_arns)
    }
  }
}

resource "aws_s3_bucket_policy" "factory" {
  bucket = aws_s3_bucket.factory.id
  policy = data.aws_iam_policy_document.factory_bucket.json

  # Same race the Flyte bucket's policy documents: both are bucket-level writes.
  depends_on = [aws_s3_bucket_public_access_block.factory]
}
```

The Terraform applier changes bucket configuration (policy, lifecycle, versioning), which are bucket actions the
data deny above does not name; it needs to be in `factory_breakglass_arns` only to read or write objects.
`aws:PrincipalArn` of an assumed role is the role's ARN, not the session's, so the role ARNs above match.

On EKS the roles are IRSA: trust `sts:AssumeRoleWithWebIdentity` from `module.eks.oidc_provider_arn` with
`:aud = sts.amazonaws.com` and `:sub = system:serviceaccount:<namespace>:<name>`, exactly the
`data.aws_iam_policy_document.trust` that `terraform-flyte`'s `flyte.tf` has, one per service account (and what
`factory_trust` above does for `terraform-union-dataplane`).

**Conditional writes.** The engine's S3 compare-and-swap is `PutObject` with `If-None-Match: *` (create only) and
`If-Match: <etag>` (replace only if unchanged). AWS supports `If-None-Match` since August 2024 and `If-Match` since
November 2024 ([announcement of enforcement](https://aws.amazon.com/about-aws/whats-new/2024/11/amazon-s3-enforcement-conditional-write-operations-general-purpose-buckets)),
so any current S3 bucket works. A refused write is `412 PreconditionFailed` or `409 ConditionalRequestConflict`,
which the engine retries after reading again. Optionally the bucket policy can *require* conditional writes with
the `s3:if-none-match` / `s3:if-match` condition keys; do not, because the browser app and `audit/` records write
unconditionally.

## GCP

`terraform-union-dataplane` has `gcp/infra/dataplane.tf` today; `terraform-flyte/gcp/` is a placeholder, so the
same file goes into it when the port lands (its README says the names and variables will match the sibling).
`google_service_account.union` and the Workload Identity binding (`module.gke.identity_namespace`) are the existing
pattern; add one Google service account each for the factory and the browser, bound to their Kubernetes service
accounts.

The variables, in `gcp/infra/variables.tf` (`name`, `region`, `project_id`, `local.labels`, `random_id.suffix`,
`google_project_service.apis` and `module.gke` are the ones the repository already has; `namespace` too if it has
one: declare it only when it does not):

```hcl
variable "factory_bucket_name" {
  description = "Name of the factory's state bucket. Null: <name>-factory-<suffix>."
  type        = string
  default     = null
}

variable "factory_noncurrent_days" {
  description = "Days a noncurrent version of a state object is kept."
  type        = number
  default     = 30
}

variable "factory_audit_days" {
  description = "Days an audit record is kept."
  type        = number
  default     = 30
}

variable "factory_kms_key_name" {
  description = "Cloud KMS key (projects/.../cryptoKeys/...) for CMEK. Null: Google-managed keys."
  type        = string
  default     = null
}

variable "factory_breakglass_principals" {
  description = "principal:// identifiers exempt from the browser/ deny policy (the applier, an operator)."
  type        = list(string)
  default     = []
}

variable "namespace" {
  description = "Kubernetes namespace of the factory's and the browser app's service accounts."
  type        = string
  default     = "union"
}

variable "factory_service_account" {
  description = "Kubernetes service account the factory's slots run under."
  type        = string
  default     = "flyte-factory"
}

variable "browser_service_account" {
  description = "Kubernetes service account of the personal browser app."
  type        = string
  default     = "factory-browser"
}
```

The bucket, service accounts and grants:

```hcl
resource "google_storage_bucket" "factory" {
  name                        = coalesce(var.factory_bucket_name, "${var.name}-factory-${random_id.suffix.hex}")
  location                    = var.region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  labels                      = local.labels

  # Google-managed encryption by default; CMEK when a key is given.
  dynamic "encryption" {
    for_each = var.factory_kms_key_name == null ? [] : [1]
    content {
      default_kms_key_name = var.factory_kms_key_name
    }
  }

  versioning {
    enabled = true
  }

  lifecycle_rule {
    condition {
      with_state                 = "ARCHIVED" # a noncurrent version
      days_since_noncurrent_time = var.factory_noncurrent_days
    }
    action {
      type = "Delete"
    }
  }

  lifecycle_rule {
    condition {
      matches_prefix = ["audit/"]
      age            = var.factory_audit_days
    }
    action {
      type = "Delete"
    }
  }

  depends_on = [google_project_service.apis]
}

resource "google_service_account" "factory" {
  account_id   = "${var.name}-factory"
  display_name = "Agent factory state"
}

resource "google_service_account" "browser" {
  account_id   = "${var.name}-browser"
  display_name = "Agent factory browser app"
}

resource "google_service_account_iam_member" "factory_workload_identity" {
  service_account_id = google_service_account.factory.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "serviceAccount:${module.gke.identity_namespace}[${var.namespace}/${var.factory_service_account}]"
}

resource "google_service_account_iam_member" "browser_workload_identity" {
  service_account_id = google_service_account.browser.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "serviceAccount:${module.gke.identity_namespace}[${var.namespace}/${var.browser_service_account}]"
}

# Reads and writes of objects are limited by the object's name:
# projects/_/buckets/<bucket>/objects/<key>.
resource "google_storage_bucket_iam_member" "factory" {
  for_each = toset(["state/", "audit/"])

  bucket = google_storage_bucket.factory.name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.factory.email}"

  condition {
    title      = "factory-${trimsuffix(each.value, "/")}"
    expression = "resource.name.startsWith(\"projects/_/buckets/${google_storage_bucket.factory.name}/objects/${each.value}\")"
  }
}

resource "google_storage_bucket_iam_member" "browser" {
  bucket = google_storage_bucket.factory.name
  role   = "roles/storage.objectUser" # read, write; no delete
  member = "serviceAccount:${google_service_account.browser.email}"

  condition {
    title      = "browser-prefix"
    expression = "resource.name.startsWith(\"projects/_/buckets/${google_storage_bucket.factory.name}/objects/browser/\")"
  }
}

# Listing is a separate, unconditional grant. `storage.objects.list` is evaluated against the bucket, not
# against an object, so a condition on resource.name never matches it and the bindings above do not let anyone
# list. A custom role with that one permission does; the identities then see every object *name* in the bucket
# (never contents: reading stays limited to the prefixes above).
resource "google_project_iam_custom_role" "factory_list" {
  role_id     = "${replace(var.name, "-", "_")}_factory_list"
  title       = "Factory bucket object lister"
  permissions = ["storage.objects.list"]
}

resource "google_storage_bucket_iam_member" "list" {
  for_each = {
    factory = google_service_account.factory.email
    browser = google_service_account.browser.email
  }

  bucket = google_storage_bucket.factory.name
  role   = google_project_iam_custom_role.factory_list.id
  member = "serviceAccount:${each.value}"
}
```

Why the separate grant: Google's [Cloud Storage IAM documentation](https://docs.cloud.google.com/storage/docs/iam)
describes `storage.objects.list` as granted at the bucket level, so a `resource.name` condition cannot confine it to
a prefix. GCS managed folders were considered as a way to scope list per prefix; Google's managed-folders page says
a policy on a folder applies to objects that use the folder path as a prefix, but does not say that list is scoped
by it, so this page does not rely on them. The consequence is stated plainly: the browser app's account can list
the names under `state/` and `audit/` (not read them), and the factory's can list the names under `browser/`.
That is acceptable because names carry no credentials: `browser/<identity>/profile.tar.gz` says an identity
exists. Do not put anything secret in an object name.

Two cautions: `roles/storage.legacyBucketReader`, which `dataplane.tf` grants Union's account so its operator can call
`buckets.get`, also lists every object name in the bucket, so it is **not** granted here (add
`roles/storage.bucketViewer` if a probe needs `buckets.get`); and a conditional binding on a bucket needs uniform
bucket-level access, which the bucket above has.

The explicit deny of reads and writes of `browser/*` to everyone else is an IAM deny policy (the bucket-level
bindings above only grant). It leaves out `objects.list` for the reason above: a list is not an object request, so the
condition cannot match it.

```hcl
resource "google_iam_deny_policy" "factory_browser_prefix" {
  parent       = urlencode("cloudresourcemanager.googleapis.com/projects/${var.project_id}")
  name         = "${var.name}-factory-browser-prefix"
  display_name = "Only the browser app reads browser/*"

  rules {
    deny_rule {
      denied_principals    = ["principalSet://goog/public:all"]
      exception_principals = concat(
        ["principal://iam.googleapis.com/projects/-/serviceAccounts/${google_service_account.browser.email}"],
        var.factory_breakglass_principals,
      )
      denied_permissions = [
        "storage.googleapis.com/objects.get",
        "storage.googleapis.com/objects.create",
        "storage.googleapis.com/objects.update",
        "storage.googleapis.com/objects.delete",
      ]
      denial_condition {
        title      = "browser prefix"
        expression = "resource.matchPrefix(\"projects/_/buckets/${google_storage_bucket.factory.name}/objects/browser/\")"
      }
    }
  }
}
```

(Deny conditions use `resource.matchPrefix`; the allow conditions above use `resource.name.startsWith`. Applying a
deny policy needs `roles/iam.denyAdmin` for the applier, which is more than `terraform-union-dataplane` asks for
today, so make this resource optional behind a variable if that matters.) GCS has no conditional-write header
dialect to configure: the engine uses generation preconditions, which every bucket supports.

## Outputs

Both repositories, both clouds, in `outputs.tf`:

| Output | Value |
|---|---|
| `factory_bucket` | the URI: `s3://<bucket>` or `gs://<bucket>`, no path prefix |
| `factory_bucket_region` | the bucket's region (`var.region`) |
| `factory_bucket_endpoint` | the S3-compatible endpoint (MinIO), for the bootstrap to write as `[state] endpoint`; only for S3-compatible servers, so empty (`""`) on AWS and GCS |
| `factory_role_arn` / `factory_service_account_email` | the factory's IRSA role, or its Google service account |
| `browser_role_arn` / `browser_service_account_email` | the browser app's |
| `factory_service_accounts` | namespace and name of the two Kubernetes service accounts, as `service_accounts` does for the existing ones |

AWS (`terraform-flyte`: the roles are `aws_iam_role.workload[...]`; `terraform-union-dataplane`: `aws_iam_role.factory`
and `aws_iam_role.browser`):

```hcl
output "factory_bucket" {
  value = "s3://${aws_s3_bucket.factory.id}"
}

output "factory_bucket_region" {
  value = var.region
}

output "factory_bucket_endpoint" {
  value = ""
}

output "factory_role_arn" {
  value = local.factory_role_arn
}

output "browser_role_arn" {
  value = local.browser_role_arn
}

output "factory_service_accounts" {
  value = {
    factory = { namespace = var.namespace, name = var.factory_service_account }
    browser = { namespace = var.namespace, name = var.browser_service_account }
  }
}
```

GCP:

```hcl
output "factory_bucket" {
  value = "gs://${google_storage_bucket.factory.name}"
}

output "factory_bucket_region" {
  value = var.region
}

output "factory_bucket_endpoint" {
  value = ""
}

output "factory_service_account_email" {
  value = google_service_account.factory.email
}

output "browser_service_account_email" {
  value = google_service_account.browser.email
}

output "factory_service_accounts" {
  value = {
    factory = { namespace = var.namespace, name = var.factory_service_account }
    browser = { namespace = var.namespace, name = var.browser_service_account }
  }
}
```

## How the bootstrap finds it

The bootstrap learns the bucket from `--state-bucket <uri>` or the `AGENT_FACTORY_STATE_BUCKET` environment
variable (or `--terraform-dir`, which reads `terraform output -json`); the value to give either is the `factory_bucket` output of the infra root
(`terraform -chdir=<cloud>/infra output -raw factory_bucket`), with `factory_bucket_region` for the region. With
`--terraform-dir` it also reads `factory_bucket_endpoint`, which matters only for an S3-compatible server. It writes
`[state] bucket` and `region` (and `endpoint`, when there is one) into the profile. With none of them set the profile stays on `git`.

## S3-compatible servers (MinIO)

Point `[state] endpoint` at the server and use the same layout. The compare-and-swap needs the server to enforce
`If-None-Match` and `If-Match` on `PutObject`; a server that ignores them lets two writers both succeed and a lease
would have two holders. MinIO documents both headers on `PutObject` in its
[S3 API compatibility list](https://docs.min.io/aistor/developers/s3-api-compatibility/); use a release that has
them (the community server since its 2024 releases; check the release notes of your build rather than assuming) and
run `tests/test_state_contract.py` against it, the way it runs against moto, before relying on it. The IAM and
lifecycle sections above are AWS features: on MinIO use `mc ilm` for the noncurrent-version expiry, `mc version
enable` and an `mc admin policy` limited to `state/*` and `audit/*`.
