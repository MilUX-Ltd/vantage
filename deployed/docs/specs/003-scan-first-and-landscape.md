# Spec 003: scan-first join, and landscape as the preferred orientation

**Slice:** UI, app side only; no box-side change. **Review tier:** standard; the gate is a
live run driven by Matt: wipe both ends, mint, render the QR, scan it with the S23, join.
**Design reference:** Spec 002, ADR-002.

## Landscape is the preferred orientation (Matt, 30 Aug 2026)

The EUD is carried on the chest in landscape. Every screen must work in both orientations
and be designed landscape-first: nothing locks portrait, no layout may assume tall-and-
narrow, and the scanner rotates freely. This is a standing product requirement from here on,
not a this-slice nicety; the deliberate low-light and glove work stays with the later
human-factors pass (carried from the estate app's backlog).

## Scan-first join

The QR scan is the primary action on Join, because that is the product story: the operator
shows a QR, the holder points the phone at it. The typed or pasted payload stays as the
first-class fallback (glare, a broken camera, no screen to show). Scanning fills the same
field and submits the same way, so both paths exercise identical validation (stale payload
refused before network, honest refusal reasons).

Scanner: the ZXing embedded capture (`com.journeyapps:zxing-android-embedded`), QR format
only, orientation unlocked, no beep (light and sound discipline point towards quiet
defaults). The capture screen requests the camera permission itself on first use.

## Acceptance

No new box-side criteria (Spec 002's suite stands). The device gate, run by Matt:

- From a wiped phone and a wiped box state, a freshly minted QR rendered on a screen is
  scanned; the app enrols, shows pending and the fingerprint; confirm at the box; status
  enrolled; Test link green.
- The same screens render usably in landscape (evidence: screenshots both orientations).
- A photograph of an already-used QR is refused with the honest single-use reason.

## Out of scope

The console mint page (still held for the parallel console work), deep-link payloads, the
sync transport, human-factors polish (type sizes, gloves, night palette).
