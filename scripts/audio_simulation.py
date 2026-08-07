from gtts import gTTS

tts = gTTS(
    text="Punto de control ENTRADA A SAUCES NORTE , Proximo punto de control ,Y DE SAUCES NORTE",
    lang="es"
)

tts.save("audio.mp3")