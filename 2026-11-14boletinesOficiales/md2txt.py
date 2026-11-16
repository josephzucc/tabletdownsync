import os
import argparse
import textwrap

def md_to_wrapped_txt(input_file, output_file, max_width=121, col_config=None):
    with open(input_file, "r", encoding="utf-8") as f:
        lines = [line.strip() for line in f if line.strip()]

    # 1. Parse the Markdown table
    table_data = []
    for line in lines:
        if "---" in line and "|" in line:
            continue
        cells = [cell.strip() for cell in line.split("|")]
        if cells[0] == "": cells.pop(0)
        if cells and cells[-1] == "": cells.pop()
        if cells:
            table_data.append(cells)

    if not table_data:
        print("No table data found.")
        return

    num_cols = max(len(row) for row in table_data)
    padding = 2

    # 2. Determine Column Widths
    col_widths = []
    
    if col_config:
        # Case A: User provided a list (e.g., "20,60,20")
        if ',' in col_config:
            try:
                col_widths = [int(x) for x in col_config.split(',')]
            except ValueError:
                print("Error: Column widths must be integers separated by commas.")
                return
        # Case B: User provided a single number for all columns
        else:
            try:
                single_width = int(col_config)
                col_widths = [single_width] * num_cols
            except ValueError:
                print("Error: Column width must be an integer.")
                return

        # Fill in missing columns if user provided fewer widths than exist in table
        while len(col_widths) < num_cols:
            col_widths.append(10) # Default fallback width

        # Validate that the total doesn't exceed the screen width
        total_w = sum(col_widths) + (padding * (num_cols - 1))
        if total_w > max_width:
            print(f"Warning: Specified widths ({total_w}) exceed max_width ({max_width}).")
            print("The text will likely wrap or be cut off on your screen.")

    else:
        # Case C: Automatic distribution (Default functionality)
        available_width = max_width - (padding * (num_cols - 1))
        single_auto_width = available_width // num_cols
        col_widths = [single_auto_width] * num_cols

    # 3. Process and Wrap Text
    with open(output_file, "w", encoding="utf-8") as f:
        for row in table_data:
            wrapped_cells = []
            for i in range(num_cols):
                # Get cell text or empty string if row is shorter than max_cols
                cell_text = row[i] if i < len(row) else ""
                # Use the specific width for this column
                width = col_widths[i] if i < len(col_widths) else 10
                wrapped_cells.append(textwrap.wrap(cell_text, width=width))

            max_lines = max(len(cell_lines) for cell_lines in wrapped_cells) if wrapped_cells else 0

            # 4. Print the row line-by-line
            for line_idx in range(max_lines):
                formatted_line = ""
                for col_idx in range(num_cols):
                    cell_lines = wrapped_cells[col_idx] if col_idx < len(wrapped_cells) else []
                    line_text = cell_lines[line_idx] if line_idx < len(cell_lines) else ""
                    
                    # Left-align to the specific column width
                    width = col_widths[col_idx] if col_idx < len(col_widths) else 10
                    formatted_line += f"{line_text:<{width}}"
                    
                    if col_idx < num_cols - 1:
                        formatted_line += " " * padding
                
                f.write(formatted_line.rstrip() + "\n")
            
            f.write("\n")

def main():
    parser = argparse.ArgumentParser(description="Convert MD table to wrapped TXT with custom widths")
    parser.add_argument("input", help="Input .md file")
    parser.add_argument("--width", type=int, default=121, help="Screen width (default 121)")
    parser.add_argument("--cols", help="Column widths: single int for all, or comma-separated (e.g. '20,60,20')")
    args = parser.parse_args()

    output_file = os.path.splitext(args.input)[0] + ".txt"
    md_to_wrapped_txt(args.input, output_file, args.width, args.cols)
    print(f"Converted {args.input} to {output_file}")

if __name__ == "__main__":
    main()
