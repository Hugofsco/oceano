---
name: aws-solution-architect
description: design an AWS architecture (serverless, three-tier, microservices) from requirements, generate CloudFormation/CDK/Terraform, and estimate/optimize costs — use when deploying something to AWS specifically
status: published
notes: ported from claude-skills engineering-team/aws-solution-architect (MIT); the upstream scripts had NO CLI at all (pure importable classes, despite the source SKILL.md documenting CLI usage) — added minimal argparse wrappers locally so they actually run; verified with real output
---
# AWS solution architect

Nothing in this setup currently deploys to AWS — reach for this only if that changes.

1. **Get a pattern recommendation:**
   `python3 skills/aws-solution-architect/scripts/architecture_designer.py --input requirements.json`
   (JSON needs `application_type`, `expected_users`, `requests_per_second`,
   `budget_monthly_usd`, `team_size`, `aws_experience`, `compliance`, `data_size_gb`)
   → pattern (serverless web / event-driven microservices / three-tier / GraphQL
   backend), service stack, cost estimate, pros/cons.
2. **Generate IaC for a serverless pattern:**
   `python3 skills/aws-solution-architect/scripts/serverless_stack.py --app-name <name> --region us-east-1 --format cloudformation|cdk|terraform`
3. **Check cost optimization opportunities:**
   `python3 skills/aws-solution-architect/scripts/cost_optimizer.py --resources inventory.json --monthly-spend 2000`
   → right-sizing, idle-resource removal, Savings Plans, storage tier transitions.

Validate any generated template before deploying: `aws cloudformation validate-template
--template-body file://template.yaml`. Never hand-roll IAM policies broader than the
resource actually needs — least privilege from the start, not a cleanup pass later.
