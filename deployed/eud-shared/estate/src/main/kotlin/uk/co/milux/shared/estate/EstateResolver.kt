package uk.co.milux.shared.estate

/**
 * One place the box's HTTP surface (ask, draft, sync) can be reached, with a human label for it.
 * Site-specific addresses are NOT baked in here: the app supplies the candidate list from its own
 * provisioned config (from slice 4, an enrolment QR), which is what keeps this module publishable.
 */
data class Endpoint(val label: String, val baseUrl: String)

/**
 * Cache-first, ordered resolution of which box endpoint an EUD should use as the network changes
 * under it.
 *
 * "Cache-first" means the endpoint that last actually worked is tried before anything else, wherever
 * it sits in the priority order, so a phone that has settled on one box does not re-probe the whole
 * list on every call. When the cached endpoint is unreachable, the candidates are tried in the order
 * the app gives them (kit LAN, then EDGE over the tailnet, then FIRMBASE over the tailnet); the first
 * reachable one wins and becomes the new last-known-good. When nothing is reachable the stale cache
 * is cleared so the next attempt starts clean.
 *
 * Reachability and persistence are injected, so this is pure logic: it unit-tests with no device, no
 * network and no Android. The app wires `reachable` to a real short-timeout probe and `cache` to
 * SharedPreferences.
 */
class EstateResolver(
    private val candidates: List<Endpoint>,
    private val reachable: (Endpoint) -> Boolean,
    private val cache: Cache,
) {
    /** Last-known-good persistence. `lastGood()` is null when nothing has worked yet. */
    interface Cache {
        fun lastGood(): String?
        fun remember(baseUrl: String)
        fun forget()
    }

    /**
     * The endpoint to use now, or null when none of the candidates is reachable. A reachable
     * last-known-good short-circuits the probe. Choosing an endpoint remembers it; finding nothing
     * reachable forgets the stale cache.
     */
    fun resolve(): Endpoint? {
        val last = cache.lastGood()
        if (last != null) {
            val cached = candidates.firstOrNull { it.baseUrl == last }
            if (cached != null && reachable(cached)) return cached
        }
        for (c in candidates) {
            if (c.baseUrl == last) continue          // already tried as the cache-first step
            if (reachable(c)) {
                cache.remember(c.baseUrl)
                return c
            }
        }
        cache.forget()
        return null
    }
}
