def read_mp3_metadata(filepath):
	"""Reads ID3v1 Title and Artist from the end of an MP3 file."""
	title, artist = "Unknown Title", "Unknown Artist"
	try:
		with open(filepath, "rb") as f:
			f.seek(-128, 2)
			tag_data = f.read(128)

			if tag_data[0:3] == b"TAG":
				raw_title = tag_data[3:33]
				raw_artist = tag_data[33:63]

				title = str(raw_title, "utf-8").strip("\x00").strip()
				artist = str(raw_artist, "utf-8").strip("\x00").strip()
	except Exception as e:
		print("Error reading MP3:", e)

	return {
		"title": title if title else "Unknown Title",
		"artist": artist if artist else "Unknown Artist",
	}


def read_wav_metadata(filepath):
	"""Parses RIFF INFO chunks for Title (INAM) and Artist (IART) in a WAV file."""
	title, artist = "Unknown Title", "Unknown Artist"
	try:
		with open(filepath, "rb") as f:
			# Verify RIFF header
			header = f.read(12)
			if header[0:4] != b"RIFF" or header[8:12] != b"WAVE":
				return {"title": title, "artist": artist}

			while True:
				chunk_id = f.read(4)
				if not chunk_id or len(chunk_id) < 4:
					break

				size_bytes = f.read(4)
				if len(size_bytes) < 4:
					break
				chunk_size = int.from_bytes(size_bytes, "little")

				if chunk_id == b"LIST":
					list_type = f.read(4)
					if list_type == b"INFO":
						bytes_read = 4
						while bytes_read < chunk_size:
							sub_id = f.read(4)
							sub_size_bytes = f.read(4)
							if len(sub_id) < 4 or len(sub_size_bytes) < 4:
								break

							sub_size = int.from_bytes(sub_size_bytes, "little")
							padding = sub_size % 2

							text_data = f.read(sub_size)
							if padding:
								f.read(padding)

							bytes_read += 8 + sub_size + padding

							if sub_id == b"INAM":
								title = str(text_data, "utf-8").strip("\x00").strip()
							elif sub_id == b"IART":
								artist = str(text_data, "utf-8").strip("\x00").strip()
						break
				else:
					f.seek(chunk_size, 1)
	except Exception as e:
		print("Error reading WAV:", e)

	return {
		"title": title if title else "Unknown Title",
		"artist": artist if artist else "Unknown Artist",
	}


def get_song_metadata(filename):
	"""Wrapper to route files based on file extension."""
	parts = filename.split(".")
	if len(parts) < 2:
		return {"title": "Unknown File", "artist": "Unknown File"}

	ext = parts[-1].lower()
	if ext == "mp3":
		return read_mp3_metadata(filename)
	elif ext == "wav":
		return read_wav_metadata(filename)
	return {"title": "Unsupported Format", "artist": "Unsupported Format"}
