#!/usr/bin/env bash
set -e

apt-get update
apt-get install -y portaudio19-dev
pip install -r requirements.txt
