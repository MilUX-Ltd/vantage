// The shared code both EUD apps agree on: the mesh wire protocol (:mesh) and the cache-first
// resolution of which box endpoint to use (:estate). Pure Kotlin, no Android, so both modules
// unit-test on the JVM without a device. Consumed by the app build as a Gradle composite build
// (see the app's settings.gradle.kts includeBuild), never vendored. Decision D3.
plugins {
    kotlin("jvm") version "2.0.21" apply false
}
subprojects {
    group = "uk.co.milux.shared"
    version = "0.1.0"
}
