---
name: azure-cloud-architect
description: design an Azure architecture (App Service, AKS, serverless), generate Bicep templates, and estimate/optimize costs — use when deploying something to Azure specifically
status: published
notes: ported from claude-skills engineering-team/azure-cloud-architect (MIT); scripts copied verbatim, stdlib-only, tested live
---
# Azure cloud architect

Nothing in this setup currently deploys to Azure — reach for this only if that changes.

1. **Get a pattern recommendation:**
   `python3 skills/azure-cloud-architect/scripts/architecture_designer.py --app-type web_app --users 10000 --requirements '{"budget_monthly_usd": 500}' --json`
   → pattern (App Service web / AKS microservices / serverless event-driven / data
   pipeline), service stack, cost estimate, pros/cons.
2. **Generate a Bicep template:**
   `python3 skills/azure-cloud-architect/scripts/bicep_generator.py --arch-type web-app|microservices --output main.bicep`
   (Bicep over ARM JSON — cleaner syntax, first-party supported, compiles to ARM.)
3. **Check cost optimization opportunities:**
   `python3 skills/azure-cloud-architect/scripts/cost_optimizer.py --config resources.json --json`

Before deploying: `az bicep build --file main.bicep` then `az deployment group validate`.
Security baseline: Managed Identity for service-to-service auth (never a stored
credential), Key Vault for every secret, Private Endpoints on PaaS services instead of
public exposure.
