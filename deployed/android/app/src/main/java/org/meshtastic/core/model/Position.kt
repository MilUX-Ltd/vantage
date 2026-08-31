package org.meshtastic.core.model

import android.os.Parcel
import android.os.Parcelable

/** Minimal Parcelable stub for Position. The bridge never invokes the IMeshService
 * methods that carry this type; it exists only so the generated AIDL interface
 * compiles. Not a faithful copy and must not be used to move real Position data. */
class Position() : Parcelable {
    constructor(p: Parcel) : this()
    override fun writeToParcel(p: Parcel, flags: Int) {}
    override fun describeContents(): Int = 0
    companion object {
        @JvmField val CREATOR = object : Parcelable.Creator<Position> {
            override fun createFromParcel(p: Parcel) = Position(p)
            override fun newArray(size: Int) = arrayOfNulls<Position>(size)
        }
    }
}
