#!/usr/bin/env python3
"""
Script to validate ROOT files and check for corruption.
"""

import sys
import argparse
from pathlib import Path

try:
    import ROOT
except ImportError:
    print("ERROR: ROOT/PyROOT is not available. Please ensure ROOT is installed and properly configured.")
    sys.exit(1)


def validate_rootfile(filepath):
    """
    Validate a ROOT file for corruption.

    Performs the following checks (ALL must pass for file to be valid):
    1. File exists on filesystem
    2. File is not empty (size > 0)
    3. File can be opened by ROOT
    4. File is not a zombie (corrupted header)
    5. File is open for reading
    6. File was not recovered via kRecovered bit (indicates improper closure)
    7. File contains at least one key/object
    8. All objects in file can be read

    Args:
        filepath: Path to the ROOT file

    Returns:
        tuple: (is_valid, message, checks_passed)
            - is_valid: True if all checks passed
            - message: Success or error message
            - checks_passed: dict with individual check results
    """
    filepath = Path(filepath)
    checks = {
        "file_exists": False,
        "non_empty": False,
        "can_open": False,
        "not_zombie": False,
        "is_open": False,
        "not_recovered_bit": False,
        "has_keys": False,
        "objects_readable": False
    }

    errors = []
    tfile = None

    # Check 1: File exists
    if filepath.exists():
        checks["file_exists"] = True
    else:
        errors.append(f"File does not exist: {filepath}")

    # Check 2: File is not empty (only if it exists)
    if checks["file_exists"]:
        if filepath.stat().st_size > 0:
            checks["non_empty"] = True
        else:
            errors.append(f"File is empty: {filepath}")

    # Check 3: File can be opened by ROOT (only if previous checks passed)
    if checks["file_exists"] and checks["non_empty"]:
        try:
            tfile = ROOT.TFile.Open(str(filepath))
            if tfile:
                checks["can_open"] = True
            else:
                errors.append(f"Failed to open file: {filepath}")
        except (OSError, Exception) as e:
            errors.append(f"Failed to open file: {filepath} ({str(e)})")

    # Check 4: File is not a zombie (only if file opened)
    if tfile and checks["can_open"]:
        if not tfile.IsZombie():
            checks["not_zombie"] = True
        else:
            errors.append(f"File is zombie (corrupted): {filepath}")

    # Check 5: File is open for reading (only if file opened and not zombie)
    if tfile and checks["can_open"] and checks["not_zombie"]:
        if tfile.IsOpen():
            checks["is_open"] = True
        else:
            errors.append(f"File is not open: {filepath}")

    # Check 6: File was not recovered (kRecovered bit check - only if file is open)
    if tfile and checks["is_open"]:
        if not tfile.TestBit(ROOT.TFile.kRecovered):
            checks["not_recovered_bit"] = True
        else:
            errors.append("File was recovered (it was likely not closed properly)")

    # Check 8: File contains at least one key/object (only if file is open)
    if tfile and checks["is_open"]:
        keys = tfile.GetListOfKeys()
        if keys and keys.GetEntries() > 0:
            checks["has_keys"] = True
        else:
            errors.append(f"File contains no keys/objects: {filepath}")

    # Check 9: All objects in file can be read (only if file has keys)
    if tfile and checks["has_keys"]:
        keys = tfile.GetListOfKeys()
        all_readable = True
        for key in keys:
            obj = key.ReadObj()
            if not obj:
                errors.append(f"Failed to read object '{key.GetName()}' from file: {filepath}")
                all_readable = False
                break
        if all_readable:
            checks["objects_readable"] = True

    # Close file if it was opened
    if tfile:
        tfile.Close()

    # Determine if file is valid
    if all(checks.values()):
        return True, "All validation checks passed", checks
    else:
        # Return the first error message, or a generic message if no specific error
        error_msg = errors[0] if errors else "Some validation checks failed"
        return False, error_msg, checks


def main():
    parser = argparse.ArgumentParser(
        description="Validate ROOT files for corruption",
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "files",
        nargs="+",
        help="ROOT file(s) to validate"
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Verbose output"
    )
    parser.add_argument(
        "-q", "--quiet",
        action="store_true",
        help="Only report invalid files"
    )

    args = parser.parse_args()

    # Suppress ROOT messages unless verbose
    if not args.verbose:
        ROOT.gErrorIgnoreLevel = ROOT.kError

    all_valid = True
    results = []

    for filepath in args.files:
        is_valid, message, checks = validate_rootfile(filepath)
        results.append((filepath, is_valid, message, checks))

        if not is_valid:
            all_valid = False
            print(f"❌ INVALID: {filepath}")
            print(f"   Reason: {message}")
            print(f"   Checks passed: {sum(checks.values())}/{len(checks)}")
            for check_name, passed in checks.items():
                status = "✓" if passed else "✗"
                print(f"     {status} {check_name}")
        elif not args.quiet:
            print(f"✓ VALID: {filepath}")
            print(f"   All checks passed: {sum(checks.values())}/{len(checks)}")

    # Summary
    if len(args.files) > 1 and not args.quiet:
        valid_count = sum(1 for _, is_valid, _, _ in results if is_valid)
        invalid_count = len(results) - valid_count
        print(f"\nSummary: {valid_count}/{len(results)} files valid, {invalid_count} invalid")

    # Exit with error code if any files are invalid
    sys.exit(0 if all_valid else 1)


if __name__ == "__main__":
    main()
