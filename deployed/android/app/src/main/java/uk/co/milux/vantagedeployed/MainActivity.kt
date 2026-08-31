package uk.co.milux.vantagedeployed

import android.content.Intent
import android.graphics.Color
import android.graphics.Typeface
import android.graphics.drawable.GradientDrawable
import android.os.Bundle
import android.view.Gravity
import android.view.View
import android.widget.Button
import android.widget.ImageButton
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity

/**
 * Vantage Deployed home: the vault's ways of working, front and centre, in the shape of the
 * estate app Matt liked. The plumbing (joining boxes, link tests, the device identity) lives
 * behind the settings gear, because an operator opens this app to work the vault, not to
 * admire the enrolment. Everything except Sync works offline against the local vault copy.
 */
class MainActivity : AppCompatActivity() {
    private val green = Color.parseColor("#113308")
    private val greyBlue = Color.parseColor("#586F7C")
    private val neutral = Color.parseColor("#F7F6EB")
    private val beige = Color.parseColor("#D2C78D")

    private lateinit var statusLine: TextView
    private lateinit var deploymentRow: LinearLayout
    private lateinit var deploymentLabel: TextView
    private lateinit var conflictCard: android.view.View

    override fun onCreate(savedInstanceState: Bundle?) {
        // The palette is hand-built for light; pin it until a deliberate night palette exists.
        androidx.appcompat.app.AppCompatDelegate.setDefaultNightMode(
            androidx.appcompat.app.AppCompatDelegate.MODE_NIGHT_NO)
        super.onCreate(savedInstanceState)
        supportActionBar?.hide()
        // A "payload" extra on the launcher intent goes straight to Join with the field
        // prefilled (companion scanner, MDM push, automated test). Prefill only.
        intent.getStringExtra("payload")?.let {
            startActivity(Intent(this, JoinActivity::class.java).putExtra("payload", it))
        }
        Thread { runCatching { DeviceIdentity.ensure() } }.start()

        val d = resources.displayMetrics.density
        val pad = (16 * d).toInt()
        val root = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(pad, pad, pad, pad)
        }
        val titleRow = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL; gravity = Gravity.CENTER_VERTICAL
        }
        titleRow.addView(TextView(this).apply {
            text = "Vantage Deployed"; textSize = 24f
            setTextColor(green); setTypeface(typeface, Typeface.BOLD)
        }, LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f))
        titleRow.addView(ImageButton(this).apply {
            setImageResource(android.R.drawable.ic_menu_manage); background = null
            contentDescription = "Settings"
            setOnClickListener {
                startActivity(Intent(this@MainActivity, SettingsActivity::class.java))
            }
        })
        root.addView(titleRow)

        statusLine = TextView(this).apply {
            textSize = 14f; setTextColor(greyBlue)
            setPadding(0, (2 * d).toInt(), 0, (4 * d).toInt())
        }
        root.addView(statusLine)

        deploymentRow = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL; gravity = Gravity.CENTER_VERTICAL
        }
        deploymentLabel = TextView(this).apply { textSize = 15f; setTextColor(green)
            setTypeface(typeface, Typeface.BOLD) }
        deploymentRow.addView(deploymentLabel,
            LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f))
        deploymentRow.addView(Button(this).apply {
            text = "Change"; textSize = 12f
            setOnClickListener {
                startActivity(Intent(this@MainActivity, DeploymentActivity::class.java))
            }
        })
        deploymentRow.setPadding(0, 0, 0, pad)
        root.addView(deploymentRow)

        root.addView(modeCard("Ask", "A grounded answer from your vault, offline") {
            withVault { startActivity(Intent(this, AskActivity::class.java)) }
        })
        root.addView(modeCard("Vault Viewer", "Search and read your notes") {
            withVault { startActivity(Intent(this, SearchActivity::class.java)) }
        })
        root.addView(modeCard("Knowledge graph", "See how the vault connects") {
            withVault { startActivity(Intent(this, GraphActivity::class.java)) }
        })
        root.addView(modeCard("Capture", "Record a person, place or note") {
            withVault {
                startActivity(Intent(this, SearchActivity::class.java)
                    .putExtra("capture", true))
            }
        })
        root.addView(modeCard("Packs", "Mission and map packs, ready for ATAK") {
            startActivity(Intent(this, PacksActivity::class.java))
        })
        root.addView(modeCard("Sync", "Bring the deployment up to date") { syncNow() })

        conflictCard = modeCard("Conflicts", "Resolve notes the box and phone both changed") {
            startActivity(Intent(this, ConflictsActivity::class.java))
        }
        root.addView(conflictCard)

        setContentView(ScrollView(this).apply { addView(root) })
    }

    override fun onResume() {
        super.onResume()
        renderStatus()
    }

    private fun enrolledBinding(): Binding? =
        Bindings.all(this).firstOrNull { it.status == "enrolled" }

    private fun renderStatus() {
        val b = enrolledBinding()
        val notes = SyncEngine.localNotes(this).size
        val staged = SyncEngine.stagedConflicts(this)
        statusLine.text = when {
            b == null && notes == 0 ->
                "Not joined to a box yet. Open settings to join one."
            b == null -> "$notes notes on this phone. No box joined; working offline."
            else -> "${b.deploymentScope.ifBlank { "Deployment" }} via ${b.box}. " +
                "$notes notes on this phone." +
                if (staged > 0) " $staged conflict(s) to resolve." else ""
        }
        conflictCard.visibility = if (staged > 0) android.view.View.VISIBLE
                                  else android.view.View.GONE
        // The deployment picker only makes sense once enrolled with at least one deployment.
        val enrolled = Prefs.enrolledLabels(this)
        deploymentRow.visibility = if (enrolled.isEmpty()) android.view.View.GONE
                                   else android.view.View.VISIBLE
        val view = Prefs.viewLabel(this)
        deploymentLabel.text = "Viewing: " + if (view == Prefs.ALL) "All deployments" else view
    }

    /** The vault screens want content; an empty phone gets pointed at Sync, not a blank list. */
    private fun withVault(go: () -> Unit) {
        if (SyncEngine.localNotes(this).isEmpty()) {
            Toast.makeText(this, "Nothing on this phone yet. Sync first.", Toast.LENGTH_SHORT).show()
            return
        }
        go()
    }

    private fun syncNow() {
        val b = enrolledBinding()
        if (b == null) {
            Toast.makeText(this, "Join a box first: settings, then Join a box.", Toast.LENGTH_LONG).show()
            return
        }
        Toast.makeText(this, "${b.box}: syncing ...", Toast.LENGTH_SHORT).show()
        Thread {
            val result = SyncEngine.sync(this, b)
            runOnUiThread {
                Toast.makeText(this, "${b.box}: ${result.line()}", Toast.LENGTH_LONG).show()
                renderStatus()
            }
        }.start()
    }

    /** A tappable mode card: bold title and a one-line descriptor, the estate home's shape. */
    private fun modeCard(title: String, desc: String, onClick: () -> Unit): View {
        val d = resources.displayMetrics.density
        val card = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding((18 * d).toInt(), (16 * d).toInt(), (18 * d).toInt(), (16 * d).toInt())
            background = GradientDrawable().apply {
                setColor(neutral); cornerRadius = 12 * d; setStroke((1 * d).toInt(), beige)
            }
            isClickable = true
            setOnClickListener { onClick() }
        }
        card.addView(TextView(this).apply {
            text = title; textSize = 18f; setTextColor(green); setTypeface(typeface, Typeface.BOLD)
        })
        card.addView(TextView(this).apply {
            text = desc; textSize = 13f; setTextColor(greyBlue); setPadding(0, (3 * d).toInt(), 0, 0)
        })
        card.layoutParams = LinearLayout.LayoutParams(
            LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT
        ).apply { bottomMargin = (12 * d).toInt() }
        return card
    }
}
