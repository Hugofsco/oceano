---
name: gcp-cloud-architect
description: design a GCP architecture (Cloud Run, GKE, BigQuery pipelines), generate Terraform/gcloud deployment scripts, and estimate/optimize costs — use when deploying something to Google Cloud specifically
status: published
notes: ported from claude-skills engineering-team/gcp-cloud-architect (MIT); scripts copied verbatim, stdlib-only, tested live
---
# GCP cloud architect

Nothing in this setup currently deploys to GCP — reach for this only if that changes.

1. **Get a pattern recommendation:**
   `python3 skills/gcp-cloud-architect/scripts/architecture_designer.py --input requirements.json`
   → pattern (serverless web on Cloud Run+Firestore / GKE microservices / data pipeline
   with Pub/Sub+Dataflow+BigQuery / ML platform on Vertex AI), service stack, cost
   estimate.
2. **Generate a deployment (Terraform + gcloud CLI):**
   `python3 skills/gcp-cloud-architect/scripts/deployment_manager.py --app-name <name> --pattern serverless_web --region us-central1`
3. **Check cost optimization opportunities:**
   `python3 skills/gcp-cloud-architect/scripts/cost_optimizer.py --resources inventory.json --monthly-spend 2000`
   → right-sizing, committed-use discounts, storage class transitions.

Before deploying: check IAM bindings follow least privilege (`gcloud projects
get-iam-policy`), secrets go through Secret Manager (never an env var visible in the
Console), and Cloud Run tasks over ~60s belong on Cloud Run's higher timeout or a
different service — Cloud Functions caps at 9 minutes.
