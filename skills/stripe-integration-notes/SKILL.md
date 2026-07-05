---
name: stripe-integration-notes
description: production Stripe patterns — idempotent webhooks, subscription proration, usage-based billing, testing with the Stripe CLI — use when building custom billing code for a web app, not for the WooCommerce store's own Stripe plugin
status: published
notes: ported from claude-skills engineering-team/stripe-integration-expert (MIT), heavily trimmed — kept the principles that generalize across frameworks, dropped the Next.js/Prisma-specific code (this is prose guidance, not copy-paste code, since your actual stack will differ)
---
# Stripe integration notes

Applies when writing *custom* billing code for a web app (a side project, not the store
itself — WooCommerce's own Stripe plugin already handles this for the store).

**Webhooks are the source of truth, not the checkout response.** Verify the signature
before trusting the payload. Keep a processed-events table/set and check it first — Stripe
retries on any non-2xx, so double-processing is the default failure mode without an
idempotency check. Return 500 (not 200) when your handling fails, so Stripe retries —
marking an event "processed" before you're sure it succeeded loses the retry.

**Webhook delivery order isn't guaranteed.** Never trust the event payload alone for
state that matters — re-fetch the subscription/invoice from the Stripe API when the
order matters.

**Proration:** preview it (`invoices.retrieveUpcoming`) before applying an upgrade and
show the user the amount — don't apply a plan change silently. Upgrades: prorate
immediately. Downgrades: apply at period end, no proration.

**Metadata discipline:** always pass your own user/order ID in checkout session
metadata — you cannot link a subscription back to a user without it.

**Test locally with the Stripe CLI** before touching a real webhook endpoint:
`stripe listen --forward-to <local-url>` then `stripe trigger checkout.session.completed`
(also: `customer.subscription.updated`, `invoice.payment_failed`). Test cards: success
`4242 4242 4242 4242`, decline `4000 0000 0000 9995`.

**Common mistakes:** customer portal not enabled in the Stripe dashboard before you try
to link to it; missing metadata on checkout; treating "no test failed" as "webhook
reliability solved" without actually simulating a duplicate delivery.
