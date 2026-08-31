package uk.co.milux.shared.mesh

import java.io.ByteArrayOutputStream
import java.nio.ByteBuffer
import java.security.MessageDigest
import java.util.zip.CRC32

/**
 * Kotlin port of the vaultsync wire format. Byte-for-byte compatible with
 * src/vaultsync/fragment.py in milux-vault-sync: the Python sender on the Linux box and this
 * receiver on the phone speak the same packets. Big-endian throughout, CRC32 trailer per packet,
 * whole-payload SHA-256 in the manifest. Shared by both EUD apps so the format cannot drift (D3).
 */
object Wire {
    val MAGIC = byteArrayOf('V'.code.toByte(), 'S'.code.toByte())
    const val VERSION = 1
    const val TYPE_MANIFEST = 0
    const val TYPE_DATA = 1
    const val TYPE_STATUS = 2
    const val KIND_FILESET = 3

    private fun crc(b: ByteArray, len: Int): Long {
        val c = CRC32(); c.update(b, 0, len); return c.value
    }

    fun seal(body: ByteArray): ByteArray {
        val out = ByteArray(body.size + 4)
        System.arraycopy(body, 0, out, 0, body.size)
        ByteBuffer.wrap(out, body.size, 4).putInt((crc(body, body.size) and 0xFFFFFFFFL).toInt())
        return out
    }

    /** Returns the body (header+payload) if the CRC checks, else null (drop it). */
    fun open(packet: ByteArray): ByteArray? {
        if (packet.size < 8 + 4) return null
        val bodyLen = packet.size - 4
        val want = ByteBuffer.wrap(packet, bodyLen, 4).int.toLong() and 0xFFFFFFFFL
        if (crc(packet, bodyLen) != want) return null
        return packet.copyOfRange(0, bodyLen)
    }
}

sealed class Packet { abstract val transferId: Long }
data class Manifest(
    override val transferId: Long, val totalFrags: Int, val totalLen: Int,
    val sha256: ByteArray, val kind: Int,
) : Packet()
data class DataFrag(override val transferId: Long, val index: Int, val chunk: ByteArray) : Packet()
data class StatusPkt(
    override val transferId: Long, val haveManifest: Boolean, val complete: Boolean,
    val missingRanges: List<IntArray>,
) : Packet() {
    fun encode(): ByteArray {
        val bb = ByteBuffer.allocate(8 + 2 + missingRanges.size * 4)
        bb.put(Wire.MAGIC); bb.put(Wire.VERSION.toByte()); bb.put(Wire.TYPE_STATUS.toByte())
        bb.putInt(transferId.toInt())
        var flags = 0
        if (haveManifest) flags = flags or 0x01
        if (complete) flags = flags or 0x02
        bb.put(flags.toByte()); bb.put(missingRanges.size.toByte())
        for (r in missingRanges) { bb.putShort(r[0].toShort()); bb.putShort(r[1].toShort()) }
        return Wire.seal(bb.array())
    }
}

object Parser {
    fun parse(packet: ByteArray): Packet? {
        val body = Wire.open(packet) ?: return null
        if (body.size < 8) return null
        val bb = ByteBuffer.wrap(body)
        val magic = ByteArray(2); bb.get(magic)
        if (!magic.contentEquals(Wire.MAGIC)) return null
        val version = bb.get().toInt(); if (version != Wire.VERSION) return null
        val type = bb.get().toInt()
        val tid = bb.int.toLong() and 0xFFFFFFFFL
        return when (type) {
            Wire.TYPE_MANIFEST -> {
                if (body.size < 8 + 2 + 4 + 32 + 1) return null
                val total = bb.short.toInt() and 0xFFFF
                val tlen = bb.int
                val sha = ByteArray(32); bb.get(sha)
                val kind = bb.get().toInt()
                Manifest(tid, total, tlen, sha, kind)
            }
            Wire.TYPE_DATA -> {
                if (body.size < 8 + 2) return null
                val index = bb.short.toInt() and 0xFFFF
                val chunk = ByteArray(bb.remaining()); bb.get(chunk)
                DataFrag(tid, index, chunk)
            }
            Wire.TYPE_STATUS -> {
                if (body.size < 8 + 2) return null
                val flags = bb.get().toInt()
                val count = bb.get().toInt() and 0xFF
                val ranges = ArrayList<IntArray>()
                for (i in 0 until count) {
                    if (bb.remaining() < 4) break
                    val s = bb.short.toInt() and 0xFFFF
                    val e = bb.short.toInt() and 0xFFFF
                    ranges.add(intArrayOf(s, e))
                }
                StatusPkt(tid, flags and 0x01 != 0, flags and 0x02 != 0, ranges)
            }
            else -> null
        }
    }
}

/** The sender half of the wire format: split a payload into a manifest packet then data packets.
 *  Byte-for-byte the mirror of src/vaultsync/fragment.py encode_transfer, so the box reassembles
 *  what the phone sends. */
object Encoder {
    fun encodeTransfer(
        payload: ByteArray, transferId: Long, kind: Int = Wire.KIND_FILESET,
        chunkSize: Int = 180, maxFrags: Int = 512,
    ): List<ByteArray> {
        require(chunkSize >= 1) { "chunkSize must be positive" }
        val total = maxOf(1, (payload.size + chunkSize - 1) / chunkSize)
        require(total <= maxFrags) { "payload needs $total fragments, over cap $maxFrags" }
        val sha = MessageDigest.getInstance("SHA-256").digest(payload)
        val out = ArrayList<ByteArray>()
        val mb = ByteBuffer.allocate(8 + 2 + 4 + 32 + 1)
        mb.put(Wire.MAGIC); mb.put(Wire.VERSION.toByte()); mb.put(Wire.TYPE_MANIFEST.toByte())
        mb.putInt(transferId.toInt())
        mb.putShort(total.toShort()); mb.putInt(payload.size); mb.put(sha); mb.put(kind.toByte())
        out.add(Wire.seal(mb.array()))
        for (i in 0 until total) {
            val start = i * chunkSize
            val chunk = payload.copyOfRange(start, minOf(start + chunkSize, payload.size))
            val db = ByteBuffer.allocate(8 + 2 + chunk.size)
            db.put(Wire.MAGIC); db.put(Wire.VERSION.toByte()); db.put(Wire.TYPE_DATA.toByte())
            db.putInt(transferId.toInt())
            db.putShort(i.toShort()); db.put(chunk)
            out.add(Wire.seal(db.array()))
        }
        return out
    }
}

/** Rebuilds one transfer from packets in any order, with duplicates and damage. */
class Reassembler(val transferId: Long, private val maxFrags: Int = 512) {
    var manifest: Manifest? = null; private set
    private val frags = HashMap<Int, ByteArray>()
    var complete = false; private set
    var integrityFailed = false; private set

    /** Returns the full payload once, when complete and the SHA matches; else null. */
    fun feed(packet: ByteArray): ByteArray? {
        val p = Parser.parse(packet) ?: return null
        if (p.transferId != transferId) return null
        when (p) {
            is Manifest -> {
                if (p.totalFrags > maxFrags) { integrityFailed = true; return null }
                if (manifest == null) manifest = p
            }
            is DataFrag -> {
                if (frags.size >= maxFrags && !frags.containsKey(p.index)) return null
                frags.putIfAbsent(p.index, p.chunk)
            }
            else -> return null
        }
        return tryComplete()
    }

    private fun tryComplete(): ByteArray? {
        val m = manifest ?: return null
        if (complete) return null
        for (i in 0 until m.totalFrags) if (!frags.containsKey(i)) return null
        val buf = ByteArrayOutputStream()
        for (i in 0 until m.totalFrags) buf.write(frags[i]!!)
        var payload = buf.toByteArray()
        if (payload.size > m.totalLen) payload = payload.copyOfRange(0, m.totalLen)
        if (payload.size != m.totalLen) { integrityFailed = true; return null }
        val digest = MessageDigest.getInstance("SHA-256").digest(payload)
        if (!digest.contentEquals(m.sha256)) { integrityFailed = true; return null }
        complete = true
        return payload
    }

    fun missing(): List<Int> {
        val m = manifest ?: return emptyList()
        return (0 until m.totalFrags).filter { !frags.containsKey(it) }
    }

    /** A single-packet status: lowest holes first, bounded to fit the radio budget. */
    fun status(maxRanges: Int = 20): StatusPkt {
        val ranges = ArrayList<IntArray>()
        for (i in missing()) {
            val last = ranges.lastOrNull()
            if (last != null && i == last[1] + 1) last[1] = i
            else { if (ranges.size == maxRanges) break; ranges.add(intArrayOf(i, i)) }
        }
        return StatusPkt(transferId, manifest != null, complete, ranges)
    }
}
