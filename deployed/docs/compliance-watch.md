# Compliance watch

Per ADR-002: this product is dev-environment, proof-of-concept and demo. No standalone
Compliance card is raised, and instead this list tracks what would need compliance attention
if the product's status changes (a customer estate, production data, or sale). Review at any
status change or at the Cyber Essentials Plus renewal (card 6181), whichever is first.

| # | Item | Why it would matter beyond demo |
|---|---|---|
| 1 | Device credential scheme (Keystore ECDSA keys, the roll) | A production credential system falls inside CE+ scope: storage, lifecycle, revocation evidence, who may write the roll. |
| 2 | Enrolment tokens and QR payloads | Short-lived credentials in transit; minting authority and audit would need stating. |
| 3 | Channel security (interim channel id, `usesCleartextTraffic`, tailnet assumption) | Production needs TLS with the pinned certificate replacing the interim channel id, and the cleartext flag removed (ADR-008). |
| 4 | User-authentication-bound signing keys | The named hardening for condition 5 if the demo posture ends. |
| 5 | Roll staleness and revocation convergence evidence | An assessor would want the bound, the degrade behaviour and its test evidence (ADR-002 sets the menu and read-only degrade). |
| 6 | Vault content on handsets | Beyond demo content, data-at-rest posture on the EUD (app-private storage, device encryption) needs a stated position. |
| 7 | Public publication of the app (vantage repo, APK on the Store shelf) | Licence, provenance of vendored code, and no estate values in the published artefact. |
