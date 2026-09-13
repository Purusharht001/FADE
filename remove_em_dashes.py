import os

def replace_em_dashes(directory):
    # Standard file extensions to check
    extensions = {'.md', '.py', '.ts', '.tsx', '.json', '.yml', '.yaml', '.toml', '.example'}
    
    count = 0
    for root, _, files in os.walk(directory):
        # Skip hidden directories and node_modules/venv
        if any(part.startswith('.') or part in ['node_modules', 'dist', '__pycache__'] for part in root.split(os.sep)):
            continue
            
        for file in files:
            if any(file.endswith(ext) for ext in extensions):
                file_path = os.path.join(root, file)
                try:
                    with open(file_path, 'r', encoding='utf-8') as f:
                        content = f.read()
                        
                    if '—' in content:
                        # Replace em dash with a standard hyphen
                        new_content = content.replace('—', '-')
                        
                        with open(file_path, 'w', encoding='utf-8') as f:
                            f.write(new_content)
                        print(f"Replaced em dashes in: {file_path}")
                        count += 1
                except Exception as e:
                    print(f"Could not process {file_path}: {e}")
                    
    print(f"\nDone! Replaced em dashes in {count} files.")

if __name__ == "__main__":
    # Run from the current directory (d:\pjt1)
    replace_em_dashes('.')
