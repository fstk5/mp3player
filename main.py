import mp3player

if __name__ == "__main__":
	player = mp3player.MP3Player()
	player.audio_manager.change_audio("forward")
	player.run()