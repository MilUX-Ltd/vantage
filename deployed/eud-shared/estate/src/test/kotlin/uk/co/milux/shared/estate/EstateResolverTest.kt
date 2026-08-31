package uk.co.milux.shared.estate

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * The three transitions the gate names, and the cache behaviour, proven as pure logic. The device
 * proof (the same resolver against real boxes over the tailnet) is in the build log; this fixes the
 * decision table so a regression is caught without a phone.
 */
class EstateResolverTest {
    private val kit = Endpoint("kit LAN", "http://kit.local:8091")
    private val edge = Endpoint("EDGE", "http://edge:8091")
    private val firmbase = Endpoint("FIRMBASE", "http://firmbase:8091")
    private val order = listOf(kit, edge, firmbase)

    private class FakeCache(var value: String? = null) : EstateResolver.Cache {
        var forgotten = false
        override fun lastGood(): String? = value
        override fun remember(baseUrl: String) { value = baseUrl }
        override fun forget() { value = null; forgotten = true }
    }

    private fun resolver(cache: FakeCache, up: Set<String>) =
        EstateResolver(order, { it.baseUrl in up }, cache)

    @Test fun transition_kit_lan_wins_when_present() {
        val cache = FakeCache()
        val chosen = resolver(cache, setOf(kit.baseUrl, edge.baseUrl, firmbase.baseUrl)).resolve()
        assertEquals(kit, chosen)
        assertEquals(kit.baseUrl, cache.value)   // remembered
    }

    @Test fun transition_tailnet_edge_when_no_kit_lan() {
        val cache = FakeCache()
        val chosen = resolver(cache, setOf(edge.baseUrl, firmbase.baseUrl)).resolve()
        assertEquals(edge, chosen)
        assertEquals(edge.baseUrl, cache.value)
    }

    @Test fun transition_firmbase_fallback_with_edge_off() {
        val cache = FakeCache()
        val chosen = resolver(cache, setOf(firmbase.baseUrl)).resolve()  // kit + edge unreachable
        assertEquals(firmbase, chosen)
        assertEquals(firmbase.baseUrl, cache.value)
    }

    @Test fun cache_first_short_circuits_the_probe() {
        // FIRMBASE is last-known-good and still up, even though kit LAN is also up now. Cache-first
        // means we stay on FIRMBASE and do not jump back to the top of the list.
        val cache = FakeCache(firmbase.baseUrl)
        val chosen = resolver(cache, setOf(kit.baseUrl, firmbase.baseUrl)).resolve()
        assertEquals(firmbase, chosen)
    }

    @Test fun stale_cache_falls_through_in_priority_order() {
        // Last-known-good was EDGE but it is off now; kit LAN is not present; FIRMBASE is. Fall
        // through to FIRMBASE and adopt it.
        val cache = FakeCache(edge.baseUrl)
        val chosen = resolver(cache, setOf(firmbase.baseUrl)).resolve()
        assertEquals(firmbase, chosen)
        assertEquals(firmbase.baseUrl, cache.value)
    }

    @Test fun nothing_reachable_returns_null_and_forgets() {
        val cache = FakeCache(edge.baseUrl)
        val chosen = resolver(cache, emptySet()).resolve()
        assertNull(chosen)
        assertTrue(cache.forgotten)
        assertNull(cache.value)
    }

    @Test fun cached_endpoint_no_longer_a_candidate_is_ignored() {
        // Config changed and dropped the endpoint the cache still names; resolution must not wedge.
        val cache = FakeCache("http://gone:8091")
        val chosen = resolver(cache, setOf(edge.baseUrl)).resolve()
        assertEquals(edge, chosen)
    }
}
