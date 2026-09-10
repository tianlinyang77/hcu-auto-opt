# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

"""BW20 child-only device guard; delegates all timing and lifecycle to original worker."""

from hcuopt.measurement.torch_worker import main as worker_main


def validate_device(torch) -> dict:
    if not torch.version.hip or not torch.cuda.is_available() or torch.cuda.device_count() != 1:
        raise RuntimeError("BW20 requires exactly one HIP device")
    properties = torch.cuda.get_device_properties(0)
    pci = (properties.pci_domain_id, properties.pci_bus_id, properties.pci_device_id)
    if pci != (0, 177, 0) or properties.gcnArchName.split(":")[0] != "gfx936":
        raise RuntimeError("BW20 physical PCI or architecture mismatch before tensor allocation")
    return {"pci": "0000:b1:00.0", "architecture": "gfx936", "logical_device_index": 0}


def main(argv=None) -> int:
    # Importing this module in the controller does not import torch or initialize HIP.
    return worker_main(argv, device_validator=validate_device)


if __name__ == "__main__":
    raise SystemExit(main())
