// Ported from milux-vault-sync android/brain GraphActivity.kt at 42a89de (ADR-001); package renamed,
// vault rebased on the product's synced app-private store.
package uk.co.milux.vantagedeployed

import android.annotation.SuppressLint
import android.os.Bundle
import android.webkit.JavascriptInterface
import android.webkit.WebView
import android.widget.TextView
import androidx.appcompat.app.AppCompatActivity
import java.io.File

/**
 * Knowledge graph of the LOCAL vault, rendered offline. Builds the graph from the phone's
 * synced vault copy and shows it in the shared force-directed renderer (bundled asset), so
 * it works with no link to the box. Colour-coded by information family, tap a node to
 * inspect it.
 */
class GraphActivity : AppCompatActivity() {
    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val vault = File(Prefs.vault(this))
        if (!vault.isDirectory) {
            setContentView(TextView(this).apply {
                text = "No local vault at ${vault.path}.\nInstall Vault Bridge and let it sync, " +
                        "or set the vault path in settings."
                setPadding(40, 80, 40, 40)
            })
            return
        }
        val json = runCatching { GraphBuilder.build(vault) }.getOrElse {
            "{\"nodes\":[],\"links\":[],\"legend\":[]}"
        }
        val tmpl = assets.open("graph.html").bufferedReader().use { it.readText() }
        // Splice the graph JSON between the markers with a literal string op. A regex
        // replacement would misread $ and \ in the JSON (paths, wikilink text) as escapes.
        val head = "/*__GRAPH__*/"; val mark = "/*__END__*/"
        val start = tmpl.indexOf(head); val end = tmpl.indexOf(mark)
        var html = if (start < 0 || end < 0 || end < start) tmpl
        else tmpl.substring(0, start) + head + json + tmpl.substring(end)
        // Start the graph filtered to the active deployment (the user can widen it in the graph).
        val dep = Prefs.deployment(this).let { f ->
            if (f.isEmpty()) "" else Deployment.labelFor(vault, f)
        }
        if (dep.isNotEmpty()) {
            val esc = dep.replace("\\", "\\\\").replace("'", "\\'")
            html = html.replace("const GRAPH =", "window.INIT_OP='" + esc + "';\nconst GRAPH =")
        }

        val web = WebView(this)
        web.settings.javaScriptEnabled = true
        web.settings.domStorageEnabled = true
        // Bridge so a node tap can open the note itself, read offline from the local vault.
        web.addJavascriptInterface(NoteBridge(vault), "Android")
        web.loadDataWithBaseURL("file:///android_asset/", html, "text/html", "utf-8", null)
        setContentView(web)
    }

    /** Lets the offline graph open a note by its vault-relative path. */
    inner class NoteBridge(private val vault: File) {
        @JavascriptInterface
        fun openNote(path: String) {
            if (path.isBlank()) return
            runOnUiThread {
                startActivity(android.content.Intent(this@GraphActivity, NoteActivity::class.java)
                    .putExtra("vault", vault.path).putExtra("path", path))
            }
        }
    }
}
