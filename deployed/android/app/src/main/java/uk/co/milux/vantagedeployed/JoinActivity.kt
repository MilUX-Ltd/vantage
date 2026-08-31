package uk.co.milux.vantagedeployed

import android.graphics.Typeface
import android.os.Bundle
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity
import com.journeyapps.barcodescanner.ScanContract
import com.journeyapps.barcodescanner.ScanOptions
import org.json.JSONObject

/**
 * Join a box (Specs 002 and 003). Scan-first: the operator shows a QR and the holder points
 * the phone at it; the typed or pasted payload stays as the first-class fallback (glare, a
 * broken camera, no screen to show). Both paths fill the same field and submit through the
 * same validation, so a stale or malformed payload is refused identically before any network
 * call. On success the device is PENDING and the holder reads the fingerprint on this screen
 * to the operator, whose confirmation at the box completes it. The payload can also arrive as
 * an intent extra ("payload") through MainActivity: prefill only; enrolment still needs the
 * press here and the operator's confirmation at the box, so this activity stays non-exported.
 */
class JoinActivity : AppCompatActivity() {
    private lateinit var input: EditText
    private lateinit var report: TextView

    private val scanLauncher = registerForActivityResult(ScanContract()) { result ->
        if (result.contents != null) {
            input.setText(result.contents)
            enrol()
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val root = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(48, 48, 48, 48)
        }
        root.addView(TextView(this).apply {
            text = "Join a box"
            textSize = 26f
            setTypeface(typeface, Typeface.BOLD)
        })
        root.addView(Button(this).apply {
            text = "Scan the enrolment QR"
            textSize = 18f
            setOnClickListener {
                scanLauncher.launch(ScanOptions().apply {
                    setDesiredBarcodeFormats(ScanOptions.QR_CODE)
                    setPrompt("Point at the enrolment QR")
                    setBeepEnabled(false)
                    setOrientationLocked(false)
                })
            }
        })
        root.addView(TextView(this).apply {
            text = "Or paste or type the code the operator gives you. Codes are single use " +
                "and expire in minutes."
            textSize = 14f
        })
        input = EditText(this).apply {
            hint = "{\"v\":1,\"t\":\"vd-enrol\", ...}"
            minLines = 3
            setText(intent.getStringExtra("payload") ?: "")
        }
        root.addView(input)
        root.addView(Button(this).apply {
            text = "Enrol this device"
            setOnClickListener { enrol() }
        })
        report = TextView(this).apply { textSize = 15f; setTextIsSelectable(true) }
        root.addView(report)
        setContentView(ScrollView(this).apply { addView(root) })
    }

    private fun say(msg: String) = runOnUiThread { report.text = msg }

    private fun enrol() {
        val obj = try { JSONObject(input.text.toString().trim()) } catch (_: Exception) { null }
        if (obj == null || obj.optInt("v") != 1 || obj.optString("t") != "vd-enrol" ||
            obj.optString("url").isBlank() || obj.optString("tok").isBlank()) {
            say("That is not an enrolment code. Ask the operator to mint one.")
            return
        }
        if (obj.optLong("exp") * 1000 < System.currentTimeMillis()) {
            say("This code has expired (they live for minutes). Ask for a fresh one.")
            return
        }
        val box = obj.optString("box", "box")
        val url = obj.optString("url").trimEnd('/')
        val pin = obj.optString("pin")
        say("Enrolling with $box ...")
        Thread {
            try {
                val r = Api.enrol(url, obj.optString("tok"))
                if (r.code == 200 && r.body.optString("status") == "enrolled") {
                    // The box already trusts this key (a rescan after Forget): no second
                    // ceremony, and the counter resumes above the box's watermark. Success
                    // returns straight to the home screen (Matt, 30 Aug): a lingering Enrol
                    // button after a done enrolment invites a second press that can only fail.
                    Bindings.put(this, Binding(box, url, pin, "enrolled",
                        r.body.optString("deployment_scope"),
                        r.body.optLong("counter_watermark")))
                    runOnUiThread {
                        Toast.makeText(this, "Enrolled with $box. Open the vault to sync.",
                            Toast.LENGTH_LONG).show()
                        finish()
                    }
                } else if (r.code == 202) {
                    Bindings.put(this, Binding(box, url, pin, "pending", "", 0))
                    runOnUiThread {
                        Toast.makeText(this,
                            "Pending with $box. Read the code on the home screen to the operator.",
                            Toast.LENGTH_LONG).show()
                        finish()
                    }
                } else {
                    say("Refused: ${r.body.optString("reason", r.code.toString())}. " +
                        "Codes are single use; ask for a fresh one.")
                }
            } catch (e: Exception) {
                say("Could not reach $box at $url (${e.javaClass.simpleName}). " +
                    "Check the phone is on the right network.")
            }
        }.start()
    }
}
