package org.meshtastic.core.model

import android.os.Parcel
import android.os.Parcelable

/** Minimal Parcelable stub for MeshUser. The bridge never invokes the IMeshService
 * methods that carry this type; it exists only so the generated AIDL interface
 * compiles. Not a faithful copy and must not be used to move real MeshUser data. */
class MeshUser() : Parcelable {
    constructor(p: Parcel) : this()
    override fun writeToParcel(p: Parcel, flags: Int) {}
    override fun describeContents(): Int = 0
    companion object {
        @JvmField val CREATOR = object : Parcelable.Creator<MeshUser> {
            override fun createFromParcel(p: Parcel) = MeshUser(p)
            override fun newArray(size: Int) = arrayOfNulls<MeshUser>(size)
        }
    }
}
