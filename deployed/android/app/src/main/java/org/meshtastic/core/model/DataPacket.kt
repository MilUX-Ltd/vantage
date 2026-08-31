package org.meshtastic.core.model

import android.os.Parcel
import android.os.Parcelable

/**
 * A faithful hand-rolled copy of the Meshtastic app's DataPacket parcel layout
 * (core/model DataPacket, v2.7.14). The Meshtastic service delivers received packets as
 * this Parcelable inside the RECEIVED broadcast, and expects the same layout on send.
 *
 * This is the ONE version-bound boundary in the bridge: the field order below mirrors
 * DataPacket.readFromParcel exactly, with `bytes` as an int length prefix plus raw
 * bytes (ByteStringParceler), MessageStatus as a presence-flag plus enum name. Verified
 * against the source read order; the write order is its inverse. Confirmed on hardware
 * before trust, per the estate's standing lesson on other people's parsers.
 */
class DataPacket : Parcelable {
    var to: String? = "^all"
    var bytes: ByteArray? = null
    var dataType: Int = 0
    var from: String? = "^local"
    var time: Long = System.currentTimeMillis()
    var id: Int = 0
    var status: String? = "UNKNOWN"   // enum name, or null
    var hopLimit: Int = 0
    var channel: Int = 0
    var wantAck: Boolean = false
    var hopStart: Int = 0
    var snr: Float = 0f
    var rssi: Int = 0
    var replyId: Int? = null
    var relayNode: Int? = null
    var relays: Int = 0
    var viaMqtt: Boolean = false
    var emoji: Int = 0
    var sfppHash: ByteArray? = null
    var transportMechanism: Int = 0

    constructor()

    constructor(to: String?, channel: Int, bytes: ByteArray?, dataType: Int) {
        this.to = to; this.channel = channel; this.bytes = bytes; this.dataType = dataType
        this.wantAck = false
    }

    constructor(p: Parcel) { readFromParcel(p) }

    /** Required by the `inout DataPacket` AIDL direction (reply unmarshalling). */
    fun readFromParcel(p: Parcel) {
        to = p.readString()
        bytes = readByteString(p)
        dataType = p.readInt()
        from = p.readString()
        time = p.readLong()
        id = p.readInt()
        status = if (p.readInt() != 0) p.readString() else null
        hopLimit = p.readInt()
        channel = p.readInt()
        wantAck = p.readInt() != 0
        hopStart = p.readInt()
        snr = p.readFloat()
        rssi = p.readInt()
        replyId = if (p.readInt() == 0) null else p.readInt()
        relayNode = if (p.readInt() == 0) null else p.readInt()
        relays = p.readInt()
        viaMqtt = p.readInt() != 0
        emoji = p.readInt()
        sfppHash = readByteString(p)
        transportMechanism = p.readInt()
    }

    override fun writeToParcel(p: Parcel, flags: Int) {
        p.writeString(to)
        writeByteString(p, bytes)
        p.writeInt(dataType)
        p.writeString(from)
        p.writeLong(time)
        p.writeInt(id)
        if (status != null) { p.writeInt(1); p.writeString(status) } else p.writeInt(0)
        p.writeInt(hopLimit)
        p.writeInt(channel)
        p.writeInt(if (wantAck) 1 else 0)
        p.writeInt(hopStart)
        p.writeFloat(snr)
        p.writeInt(rssi)
        if (replyId == null) p.writeInt(0) else { p.writeInt(1); p.writeInt(replyId!!) }
        if (relayNode == null) p.writeInt(0) else { p.writeInt(1); p.writeInt(relayNode!!) }
        p.writeInt(relays)
        p.writeInt(if (viaMqtt) 1 else 0)
        p.writeInt(emoji)
        writeByteString(p, sfppHash)
        p.writeInt(transportMechanism)
    }

    override fun describeContents(): Int = 0

    companion object {
        // ByteStringParceler in the app uses Parcel.writeByteArray / createByteArray:
        // an int length (or -1 for null) followed by the raw bytes. Match exactly.
        private fun readByteString(p: Parcel): ByteArray? = p.createByteArray()
        private fun writeByteString(p: Parcel, b: ByteArray?) = p.writeByteArray(b)

        @JvmField
        val CREATOR = object : Parcelable.Creator<DataPacket> {
            override fun createFromParcel(p: Parcel) = DataPacket(p)
            override fun newArray(size: Int) = arrayOfNulls<DataPacket>(size)
        }
    }
}
