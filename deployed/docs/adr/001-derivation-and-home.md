# ADR-001: Vantage Deployed derives from the Deployed builds and lives in the development repository

**Status:** Accepted (Matt, 30 Aug 2026). **Owner:** Matt.

## Context

The Deployed app (`milux-vault-sync/android`, one APK per ADR-009) is proven on the MilUX
estate but is provisioned by hand: three URL fields typed into the app, box-side state deployed
by scp. Matt's intent is a productised app any Android user can join to a Vantage-managed
estate by scanning a QR code, published for sharing on the public `MilUX-Ltd/vantage` repo.
The live Deployed app and its Meshtastic build must not break while this is built.

## Decision

1. **A new app, Vantage Deployed**, derived from the Deployed builds, not a mutation of them.
   The estate's app keeps running until this product reaches parity and Matt cuts over.
2. **Development home is `the development repository`**, in this `vantage-deployed/` subtree, because every
   feature spans the console (mints the QR), the box (serves enrolment and sync) and the app
   (joins): one PR per feature under the repo's trunk-and-PR workflow. The public
   `MilUX-Ltd/vantage` repo stays the publish surface, fed by the tarball cut.
3. **Box-side code is vendored, not imported across repos.** The product must ship
   self-contained. The proven modules are copied from `milux-vault-sync` at commit `42a89de`
   (`roll.py`, `signing.py`, verbatim plus a provenance header) and owned here from now on;
   `milux-vault-sync` remains the MilUX estate instance, mirroring the product-versus-estate
   split the console made. New product code (enrolment) is written here.
4. **The python package keeps the name `vaultsync`** until the Vantage 2.0 namespace
   migration renames the product's surfaces in one deliberate breaking release (an internal card's
   interim rule: new files take vantage names, existing names do not churn; the package name is
   inherited, not new).
5. **The Meshtastic mesh bearer is kept**, capability-gated: offered when a bound box has a
   mesh radio, invisible otherwise. It is the product's differentiator and the code arrives
   with the app derivation.
6. **`milux-eud-shared` stays its own repository** (Kit Check shares it), pulled in by Gradle
   composite build; its source is vendored into the public cut at publish time.

## Consequences

Two copies of `roll.py` and `signing.py` exist until the estate cuts over; the product copy is
canonical from this ADR forward and the estate copy is frozen except for defect fixes, which
land here first. The Compliance card for the credential scheme (the Keystore key under CE+)
must be answered before enrolment goes live on a real estate.
