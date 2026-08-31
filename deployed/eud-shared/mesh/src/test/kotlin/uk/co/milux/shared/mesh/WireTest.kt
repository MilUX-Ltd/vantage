package uk.co.milux.shared.mesh

import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * The wire format's contract, proven on the JVM: encode then reassemble round-trips, packets
 * survive any order and duplication, a damaged CRC is dropped, and a corrupted payload is caught
 * by the manifest SHA. This is the format the box and every EUD must agree on, so it is tested
 * here in the shared module, not in either app.
 */
class WireTest {
    private fun payload(n: Int) = ByteArray(n) { ((it * 31 + 7) and 0xFF).toByte() }

    @Test fun round_trip_in_order() {
        val data = payload(1000)
        val packets = Encoder.encodeTransfer(data, transferId = 42)
        val r = Reassembler(42)
        var out: ByteArray? = null
        for (p in packets) out = r.feed(p) ?: out
        assertTrue(r.complete)
        assertArrayEquals(data, out)
    }

    @Test fun round_trip_shuffled_and_duplicated() {
        val data = payload(1500)
        val packets = Encoder.encodeTransfer(data, transferId = 7).shuffled(java.util.Random(1))
        val r = Reassembler(7)
        var out: ByteArray? = null
        for (p in packets) { out = r.feed(p) ?: out; out?.let { r.feed(p) } } // feed some twice
        assertArrayEquals(data, out)
    }

    @Test fun damaged_crc_is_dropped() {
        val packets = Encoder.encodeTransfer(payload(400), transferId = 9)
        val bad = packets[1].copyOf()
        bad[bad.size - 1] = (bad[bad.size - 1] + 1).toByte()   // flip a CRC byte
        assertNull(Parser.parse(bad))
    }

    @Test fun corrupted_payload_is_caught_by_the_manifest_sha() {
        val data = payload(360)
        val packets = Encoder.encodeTransfer(data, transferId = 5).toMutableList()
        // Corrupt one data chunk but keep its CRC valid, so only the whole-payload SHA can catch it.
        val frag = packets[1]
        val body = frag.copyOfRange(0, frag.size - 4)
        body[body.size - 1] = (body[body.size - 1].toInt() xor 0xFF).toByte()
        packets[1] = Wire.seal(body)
        val r = Reassembler(5)
        var out: ByteArray? = null
        for (p in packets) out = r.feed(p) ?: out
        assertNull(out)
        assertFalse(r.complete)
        assertTrue(r.integrityFailed)
    }

    @Test fun sender_repairs_exactly_the_missing_fragments() {
        val data = payload(2000)
        val received = Reassembler(11)
        var delivered = false
        val sender = MeshSender(11, data, send = { pkt -> received.feed(pkt) }, onDone = { delivered = it })
        // First pass drops odd-indexed data fragments to simulate a lossy link.
        val all = Encoder.encodeTransfer(data, 11)
        received.feed(all[0])                                   // manifest
        for (i in 1 until all.size) if (i % 2 == 0) received.feed(all[i])
        // The box reports what it still lacks and the sender fills exactly those holes; once the box
        // is whole it reports complete, and only then does the sender count the transfer delivered.
        var rounds = 0
        while (!delivered && rounds++ < 50) sender.onStatus(received.status())
        assertTrue(received.complete)
        assertTrue(delivered)
    }
}
