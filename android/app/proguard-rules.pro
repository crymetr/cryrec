# ML Kit finds its parts by class name from the manifest and creates them with the no-argument constructor.
# R8 (full mode) otherwise removes those constructors and the document scanner crashes in release builds.
-keep class * implements com.google.firebase.components.ComponentRegistrar { <init>(); }
-keep class com.google.mlkit.common.internal.** { <init>(...); }
-keep class com.google.mlkit.vision.documentscanner.** { *; }
