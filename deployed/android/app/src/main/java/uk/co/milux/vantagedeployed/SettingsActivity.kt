package uk.co.milux.vantagedeployed

import android.content.Intent
import android.graphics.Typeface
import android.os.Bundle
import android.view.Gravity
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.Switch
import android.widget.ScrollView
import android.widget.TextView
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity

/**
 * Settings: the plumbing that used to squat on the home screen. The boxes this phone is
 * bound to (join, status, link test, forget), the device identity, and the app version.
 * The vault's ways of working live on the home screen where the operator starts.
 */
class SettingsActivity : AppCompatActivity() {
    private lateinit var listView: LinearLayout

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        title = "Settings"
        val root = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(48, 48, 48, 48)
        }
        root.addView(TextView(this).apply {
            text = "Device ${DeviceIdentity.deviceId().take(12)}  " +
                if (DeviceIdentity.inSecureHardware()) "(secure hardware)" else "(software keystore)"
            textSize = 13f
        })
        root.addView(TextView(this).apply {
            text = "Vantage Deployed ${packageManager.getPackageInfo(packageName, 0).versionName}"
            textSize = 13f
        })
        root.addView(TextView(this).apply {
            text = "\nBoxes"
            textSize = 18f
            setTypeface(typeface, Typeface.BOLD)
        })
        listView = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }
        root.addView(listView)
        root.addView(Button(this).apply {
            text = "Join a box"
            setOnClickListener {
                startActivity(Intent(this@SettingsActivity, JoinActivity::class.java))
            }
        })
        root.addView(TextView(this).apply {
            text = "\nOffline AI model"
            textSize = 18f
            setTypeface(typeface, Typeface.BOLD)
        })
        root.addView(TextView(this).apply {
            val present = OnDeviceModel.available(this@SettingsActivity)
            text = if (present) "Model installed. Ask answers on this phone, offline."
                   else "No model. Ask finds the closest notes but cannot write an answer. " +
                        "Provision a .task or .litertlm model (0.5 to 1.5 GB) into " +
                        "Android/data/uk.co.milux.vantagedeployed/files/models/ at kitting."
            textSize = 13f
        })
        root.addView(TextView(this).apply {
            text = "\nMesh bearer (LoRa via Meshtastic)"
            textSize = 18f
            setTypeface(typeface, Typeface.BOLD)
        })
        root.addView(TextView(this).apply {
            text = "For kits with radios: vault content arrives over the mesh when there is " +
                "no network. Needs the Meshtastic app connected to this phone's radio."
            textSize = 13f
        })
        val meshOn = Switch(this).apply {
            text = "Receive over the mesh"
            isChecked = MeshPrefs.enabled(this@SettingsActivity)
        }
        val meshPort = EditText(this).apply {
            hint = "Mesh port (default ${MeshPrefs.DEFAULT_PORT})"
            inputType = android.text.InputType.TYPE_CLASS_NUMBER
            setText(MeshPrefs.port(this@SettingsActivity).toString())
        }
        val meshReverse = Switch(this).apply {
            text = "Send edits over the mesh when off WiFi"
            isChecked = MeshPrefs.reverse(this@SettingsActivity)
        }
        root.addView(meshOn); root.addView(meshPort); root.addView(meshReverse)
        root.addView(Button(this).apply {
            text = "Apply mesh settings"
            setOnClickListener {
                val port = meshPort.text.toString().toIntOrNull() ?: MeshPrefs.DEFAULT_PORT
                MeshPrefs.set(this@SettingsActivity, meshOn.isChecked, port, meshReverse.isChecked)
                val svc = Intent(this@SettingsActivity, MeshBearerService::class.java)
                if (meshOn.isChecked) {
                    startForegroundService(svc)
                    Toast.makeText(this@SettingsActivity,
                        "Mesh bearer on (port $port): ${MeshBearerService.status}",
                        Toast.LENGTH_LONG).show()
                } else {
                    stopService(svc)
                    Toast.makeText(this@SettingsActivity, "Mesh bearer off", Toast.LENGTH_SHORT).show()
                }
            }
        })
        setContentView(ScrollView(this).apply { addView(root) })
    }

    override fun onResume() {
        super.onResume()
        render()
    }

    private fun render() {
        listView.removeAllViews()
        val bindings = Bindings.all(this)
        if (bindings.isEmpty()) {
            listView.addView(TextView(this).apply {
                text = "\nNo boxes joined yet. Ask the operator for an enrolment QR, " +
                    "then Join a box.\n"
                textSize = 15f
            })
            return
        }
        bindings.forEach { b ->
            listView.addView(TextView(this).apply {
                text = "\n${b.box}  [${b.status}]" +
                    if (b.deploymentScope.isNotBlank()) "  ${b.deploymentScope}" else ""
                textSize = 17f
                setTypeface(typeface, Typeface.BOLD)
            })
            listView.addView(TextView(this).apply { text = b.url; textSize = 12f })
            if (b.status == "pending") {
                listView.addView(TextView(this).apply {
                    text = "Read this code to the operator, then Check status:"
                    textSize = 14f
                })
                listView.addView(TextView(this).apply {
                    text = PairingCode.display(DeviceIdentity.deviceId())
                    textSize = 44f
                    setTypeface(Typeface.MONOSPACE, Typeface.BOLD)
                })
            }
            val row = LinearLayout(this).apply {
                orientation = LinearLayout.HORIZONTAL; gravity = Gravity.START
            }
            row.addView(Button(this).apply {
                text = "Check status"
                setOnClickListener { checkStatus(b) }
            })
            row.addView(Button(this).apply {
                text = "Test link"
                setOnClickListener { testLink(b) }
            })
            row.addView(Button(this).apply {
                text = "Forget"
                setOnClickListener {
                    Bindings.forget(this@SettingsActivity, b.url)
                    render()
                }
            })
            listView.addView(row)
        }
    }

    private fun toast(msg: String) = runOnUiThread {
        Toast.makeText(this, msg, Toast.LENGTH_LONG).show()
    }

    private fun checkStatus(b: Binding) {
        Thread {
            try {
                val r = Api.status(b.url)
                if (r.code == 200) {
                    b.status = r.body.optString("status", b.status)
                    b.deploymentScope = r.body.optString("deployment_scope", b.deploymentScope)
                    // Resume above the box's watermark (heals a re-join after Forget).
                    b.counter = maxOf(b.counter, r.body.optLong("counter_watermark"))
                    Bindings.update(this, b)
                    toast("${b.box}: ${b.status}")
                } else {
                    toast("${b.box}: ${r.code} ${r.body.optString("reason")}")
                }
            } catch (e: Exception) {
                toast("${b.box}: unreachable (${e.javaClass.simpleName})")
            }
            runOnUiThread { render() }
        }.start()
    }

    private fun testLink(b: Binding) {
        Thread {
            try {
                val r = Api.ping(this, b)
                if (r.code == 200) {
                    toast("${b.box}: link good, signed as ${r.body.optString("label")}")
                } else {
                    toast("${b.box}: refused ${r.code} ${r.body.optString("reason")}")
                }
            } catch (e: Exception) {
                toast("${b.box}: unreachable (${e.javaClass.simpleName})")
            }
        }.start()
    }
}
