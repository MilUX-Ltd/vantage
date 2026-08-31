// Ported from milux-vault-sync android/brain OnDeviceModel.kt at 42a89de (ADR-001).
package uk.co.milux.vantagedeployed

import android.content.Context
import com.google.mediapipe.tasks.genai.llminference.LlmInference
import java.io.File

/**
 * The on-device model, fully offline (slice 2 of the on-device AI design). Wraps the MediaPipe
 * LLM Inference runtime. The model file is NOT bundled in the APK: it is provisioned onto the
 * handset at kitting (a `.task`/`.litertlm` file, Gemma-class) and everything here gates on the
 * file being present, so with no model the app simply falls back to retrieval-only and never
 * breaks. Nothing is fetched at runtime; the model loads from local storage with every radio off.
 *
 * Provisioning: put the model at `Android/data/uk.co.milux.vantagedeployed/files/models/model.task`
 * (adb push, or copied from the box over the kit LAN at kitting).
 */
object OnDeviceModel {
    private var llm: LlmInference? = null
    private var triedPath: String? = null

    /** The provisioned model: either bundle format. The loader sniffs by extension, so the file
     *  must keep the extension it shipped with (.task is a zip; .litertlm is not). */
    fun modelFile(c: Context): File {
        val dir = File(c.getExternalFilesDir(null), "models")
        for (name in listOf("model.litertlm", "model.task")) {
            val f = File(dir, name)
            if (f.isFile) return f
        }
        return File(dir, "model.task")
    }

    fun available(c: Context): Boolean = modelFile(c).isFile

    /** Lazily create the inference engine; returns null if the model is absent or fails to load. */
    @Synchronized
    private fun engine(c: Context): LlmInference? {
        val f = modelFile(c)
        if (!f.isFile) return null
        if (llm != null && triedPath == f.path) return llm
        llm = runCatching {
            LlmInference.createFromOptions(
                c.applicationContext,
                LlmInference.LlmInferenceOptions.builder()
                    .setModelPath(f.path)
                    // Total budget, input AND output. A grounded prompt is ~1.2k tokens; 2560 leaves
                    // room to answer while keeping the KV cache small enough for an 8GB phone
                    // (3584 tipped the low-memory killer during a draft).
                    .setMaxTokens(2560)
                    .build(),
            )
        }.getOrNull()
        triedPath = f.path
        return llm
    }

    /**
     * Grounded generation, blocking (call off the main thread). Returns null when no model is
     * provisioned or the engine failed, so the caller falls back to retrieval-only.
     *
     * Reasoning models (Qwen3) are asked not to think aloud (`/no_think`, harmless to others) and
     * any `<think>` block that leaks anyway is stripped, so the operator sees the answer, not the
     * model's workings.
     */
    fun generate(c: Context, prompt: String): String? {
        val e = engine(c) ?: return null
        val raw = runCatching { e.generateResponse(prompt + " /no_think") }.getOrNull() ?: return null
        return stripThink(raw).trim().takeIf { it.isNotEmpty() }
    }

    /** Remove a leaked chain-of-thought block; deterministic containment, mirrors the box. */
    fun stripThink(text: String): String =
        text.replace(Regex("(?s)<think>.*?</think>"), "").replace(Regex("(?s)^.*</think>"), "").trim()
}
