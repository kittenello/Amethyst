import onnxruntime as ort

print(ort.get_available_providers())

if "DmlExecutionProvider" in ort.get_available_providers():
    import torch_directml

    for i in range(torch_directml.device_count()):
        print(f"{i}: {torch_directml.device_name(i)}")
else:
    print("error")