package uk.co.milux.vantagedeployed

import android.graphics.Color
import android.graphics.Typeface
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.view.View
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.ProgressBar
import android.widget.ScrollView
import android.widget.TextView
import androidx.appcompat.app.AppCompatActivity
import java.io.File

/**
 * Ask: a grounded question answered from the phone's own vault, entirely offline. Retrieval
 * (BM25 over the local vault, clearance-filtered fail-closed) always runs; if an on-device
 * model is provisioned it writes the answer from the retrieved context, otherwise the closest
 * notes are shown, so the feature degrades to a recall aid and never breaks. Scoped to the
 * chosen view deployment, so an answer is drawn from the operation the operator is looking at.
 * A box-assisted tier (an edge model on the box) is a later, signed addition; this is the
 * self-sufficient offline core.
 */
class AskActivity : AppCompatActivity() {
    private val h = Handler(Looper.getMainLooper())
    private val green = Color.parseColor("#113308")
    private val greyBlue = Color.parseColor("#586F7C")
    private var inFlight = false

    private lateinit var box: EditText
    private lateinit var askBtn: Button
    private lateinit var progress: ProgressBar
    private lateinit var answer: TextView
    private lateinit var sources: TextView

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        title = "Ask"
        val d = resources.displayMetrics.density
        val pad = (16 * d).toInt()
        val root = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL; setPadding(pad, pad, pad, pad)
        }
        root.addView(TextView(this).apply {
            text = "Ask your vault"; textSize = 22f; setTextColor(green)
            setTypeface(typeface, Typeface.BOLD)
        })
        val model = OnDeviceModel.available(this)
        root.addView(TextView(this).apply {
            text = if (model) "Answered on this phone, offline, from the notes you hold."
                   else "No model on this phone yet, so this finds the closest notes rather " +
                        "than writing an answer. Provision a model in Settings to answer offline."
            textSize = 13f; setTextColor(greyBlue); setPadding(0, (2 * d).toInt(), 0, pad)
        })
        box = EditText(this).apply { hint = "e.g. where is RV ALPHA?"; minLines = 2 }
        root.addView(box)
        askBtn = Button(this).apply { text = "Ask"; setOnClickListener { ask() } }
        root.addView(askBtn)
        progress = ProgressBar(this).apply { visibility = View.GONE }
        root.addView(progress)
        answer = TextView(this).apply {
            textSize = 16f; setTextColor(green); setPadding(0, pad, 0, 0); setTextIsSelectable(true)
        }
        root.addView(answer)
        sources = TextView(this).apply { textSize = 12f; setTextColor(greyBlue) }
        root.addView(sources)
        setContentView(ScrollView(this).apply { addView(root) })
    }

    private fun ask() {
        val q = box.text.toString().trim()
        if (q.isEmpty() || inFlight) return
        inFlight = true; askBtn.isEnabled = false; progress.visibility = View.VISIBLE
        answer.text = ""; sources.text = ""
        Thread {
            val outcome = runCatching { offlineAnswer(q) }
            h.post {
                inFlight = false; progress.visibility = View.GONE; askBtn.isEnabled = true
                outcome.onSuccess { (ans, srcs) ->
                    answer.text = ans
                    sources.text = if (srcs.isEmpty()) "" else "Sources: " + srcs.joinToString(", ")
                }.onFailure {
                    answer.text = "No answer. ${it.message}"
                }
            }
        }.start()
    }

    /** Retrieval at OFFICIAL over the chosen deployment; on-device generation if a model is present. */
    private fun offlineAnswer(q: String): Pair<String, List<String>> {
        val vault = File(Prefs.vault(this))
        if (!vault.isDirectory) throw IllegalStateException("Nothing on this phone yet. Sync first.")
        val dep = Prefs.deployment(this)   // the chosen view deployment folder, or "" for all
        var chunks = Retriever.loadVault(vault)
        if (dep.isNotEmpty()) chunks = chunks.filter { it.note.startsWith("$dep/") }
        val hits = Retriever.Index(chunks).search(q, k = 5, clearance = "OFFICIAL")
        if (hits.isEmpty()) return Pair(
            "Nothing in this deployment matches that. Try a callsign or place name as it is " +
                "written in the notes.", emptyList())
        OnDeviceModel.generate(this, Retriever.operatorContext(vault) + Retriever.buildPrompt(q, hits))
            ?.let { return Pair("Answered on this phone, offline.\n\n$it", Retriever.sources(hits)) }
        val body = hits.joinToString("\n\n") { (c, _) ->
            val head = if (c.heading.isNotEmpty()) " (${c.heading})" else ""
            "• ${c.note}$head\n${c.text.trim()}"
        }
        return Pair("No model on this phone, so these are the closest notes:\n\n$body",
            Retriever.sources(hits))
    }
}
