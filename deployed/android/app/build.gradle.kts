plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}
android {
    namespace = "uk.co.milux.vantagedeployed"
    compileSdk = 35
    defaultConfig {
        applicationId = "uk.co.milux.vantagedeployed"
        minSdk = 26
        targetSdk = 34
        versionCode = 10
        versionName = "0.5.0"
    }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions { jvmTarget = "17" }
    buildFeatures { aidl = true }
}
dependencies {
    // Canonical request signing, shared with the box so both agree on what is signed (ADR-008).
    implementation("uk.co.milux.shared:identity")
    // The mesh wire protocol, shared with the box (fragment/repair, field-proven on the estate).
    implementation("uk.co.milux.shared:mesh")
    implementation("androidx.appcompat:appcompat:1.7.0")
    // QR capture for scan-first join (Spec 003). The capture screen handles the camera
    // permission itself and rotates freely, matching the landscape-first requirement.
    implementation("com.journeyapps:zxing-android-embedded:4.3.0")
    // On-device LLM runtime (MediaPipe LLM Inference). The model file is NOT bundled; the
    // code gates on its presence and falls back to retrieval-only when absent.
    implementation("com.google.mediapipe:tasks-genai:0.10.35")
    testImplementation("junit:junit:4.13.2")
}
