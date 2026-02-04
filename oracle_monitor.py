#!/usr/bin/env python3
"""
Oracle Monitor CLI Entry Point
Run with: python -m oracle_monitor or install with pip
"""

import sys
import os

# Add the parent directory to path for package imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.oracle.cli import main

if __name__ == "__main__":
    main()
