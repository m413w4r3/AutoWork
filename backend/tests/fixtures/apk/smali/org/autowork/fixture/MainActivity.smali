.class public Lorg/autowork/fixture/MainActivity;
.super Landroid/app/Activity;
.source "MainActivity.java"

.method public constructor <init>()V
    .locals 0
    invoke-direct {p0}, Landroid/app/Activity;-><init>()V
    return-void
.end method

.method public static fixtureUrl()Ljava/lang/String;
    .locals 1
    const-string v0, "https://fixture.example.invalid/"
    return-object v0
.end method

.method public static fixtureMarker()Ljava/lang/String;
    .locals 1
    const-string v0, "AUTOWORK_APK_FIXTURE"
    return-object v0
.end method
