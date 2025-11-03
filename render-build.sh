#!/usr/bin/env bash
set -e  # stop if any command fails

# echo "🚀 Starting Render build script..."

# # Update system packages
# apt-get update -y
# echo "✅ apt-get update complete"

# # Install PortAudio (required for PyAudio)
# apt-get install -y portaudio19-dev
# echo "✅ PortAudio installed"

# # Verify PortAudio was installed
# ldconfig -p | grep portaudio || echo "⚠️ PortAudio not found in system libraries"

# Install Python dependencies
pip install -r requirements.txt
echo "✅ Python dependencies installed"

echo "🎉 Render build script finished successfully!"
