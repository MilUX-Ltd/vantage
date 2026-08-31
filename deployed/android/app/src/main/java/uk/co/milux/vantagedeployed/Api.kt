package uk.co.milux.vantagedeployed

import android.content.Context
import android.util.Base64
import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URL

/**
 * The box's HTTP surface (Specs 002 and 004), from the phone's side. Plain HttpURLConnection,
 * short timeouts, every call off the main thread (callers wrap in a Thread). Signed requests
 * bind the per-binding counter and channel pin into the signature; the counter is taken
 * through Bindings.nextCounter, persisted before use, so a crash cannot replay a value.
 */
object Api {
    data class Reply(val code: Int, val bytes: ByteArray) {
        val body: JSONObject
            get() = try {
                val t = bytes.toString(Charsets.UTF_8)
                if (t.isBlank()) JSONObject() else JSONObject(t)
            } catch (_: Exception) { JSONObject() }
    }

    private fun request(method: String, url: String, body: ByteArray?,
                        headers: Map<String, String> = emptyMap()): Reply {
        val conn = URL(url).openConnection() as HttpURLConnection
        return try {
            conn.requestMethod = method
            conn.connectTimeout = 4000
            conn.readTimeout = 10000
            headers.forEach { (k, v) -> conn.setRequestProperty(k, v) }
            if (body != null) {
                conn.doOutput = true
                conn.setRequestProperty("Content-Type", "application/json")
                conn.outputStream.use { it.write(body) }
            }
            val code = conn.responseCode
            val data = (if (code in 200..299) conn.inputStream else conn.errorStream)
                ?.use { it.readBytes() } ?: ByteArray(0)
            Reply(code, data)
        } finally {
            conn.disconnect()
        }
    }

    /** One signed request against a binding's box: the transport's only shape (Spec 004).
     *  `pathWithQuery` is inside the signature, so the file path and base hash are
     *  tamper-evident; the persisted counter advances even on failure, never reused. */
    fun signed(c: Context, binding: Binding, method: String, pathWithQuery: String,
               body: ByteArray? = null): Reply {
        val counter = Bindings.nextCounter(c, binding.url)
        val payload = body ?: ByteArray(0)
        val sig = DeviceIdentity.sign(method, pathWithQuery,
            if (method == "GET") ByteArray(0) else payload, counter, "", binding.pin)
        return request(method, "${binding.url}$pathWithQuery",
            if (method == "GET") null else payload, mapOf(
                "X-VD-Device" to DeviceIdentity.deviceId(),
                "X-VD-Counter" to counter.toString(),
                "X-VD-Signature" to Base64.encodeToString(sig, Base64.NO_WRAP),
            ))
    }

    /** Present the token and this device's public key. 202 pending on success. */
    fun enrol(baseUrl: String, token: String): Reply {
        val key = Base64.encodeToString(DeviceIdentity.publicKeyDer(), Base64.NO_WRAP)
        val body = JSONObject().put("tok", token).put("key", key).toString().toByteArray()
        return request("POST", "$baseUrl/enrol", body)
    }

    fun status(baseUrl: String): Reply =
        request("GET", "$baseUrl/enrol/status/${DeviceIdentity.deviceId()}", null)

    /** The first signed request: proves the enrolled identity end to end. */
    fun ping(c: Context, binding: Binding): Reply =
        signed(c, binding, "POST", "/sync/ping", "{}".toByteArray())
}
