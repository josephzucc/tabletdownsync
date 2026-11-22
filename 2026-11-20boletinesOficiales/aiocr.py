def main():
    parser = argparse.ArgumentParser(description="VLM Image to Text via llama-server")
    parser.add_argument("image", help="Path to the image file")
    parser.add_argument("--mode", choices=["table", "doc"], default="table", 
                        help=" 'table' for Markdown tables, 'doc' for general document transcription")

    # These arguments are now optional and ignored,
    # so your old bash commands won't crash the script.
    parser.add_argument("--model", help="Ignored: Model is already loaded in server")
    parser.add_argument("--clip", help="Ignored: Clip is already loaded in server")

    args = parser.parse_args()

    # Prompt selection based on the type of document
    if args.mode == "table":
        prompt = (
            "You are a professional OCR system. Transcribe the table in this image "
            "exactly as it appears. Use Markdown table format. The language is Spanish; "
            "preserve all accents (á, é, í, ó, ú) and the letter 'ñ'. "
            "Output ONLY the raw Markdown table."
        )
    else:
        prompt = (
            "You are a professional document digitizer. Transcribe all the text in this image. "
            "Maintain the visual structure. If there are tables, use Markdown. "
            "The language is Spanish; preserve all accents and the letter 'ñ'. "
            "Output the transcription exactly as it appears."
        )

    result = process_image(args.image, prompt)
    
    # Save result to .md file
    output_file = os.path.splitext(args.image)[0] + ".md"
    with open(output_file, "w", encoding="utf-8") as f:
        f.write(result)
    
    print(f"Successfully saved result to {output_file}")
    print("\n--- Preview ---\n")
    print(result)
