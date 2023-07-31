from os import path
from examples.compile_efficientnet import compile_net, jit_model
from models.efficientnet import EfficientNet
from tinygrad.state import get_state_dict, safe_save
from tinygrad.tensor import Tensor

if __name__ == "__main__":
  model = EfficientNet(0)
  model.load_from_pretrained()
  run, special_names = jit_model(model, Tensor.randn(1,3,224,224))
  functions, statements, bufs, _bufs_to_save = compile_net(run, special_names)
  
  state = get_state_dict(model)
  weights = {id(x.lazydata.realized): name for name, x in state.items()}
  safe_save(state, path.join(path.dirname(__file__), "net.safetensors"))

  kernel_code = '\n\n'.join([f"const {key} = `{code.replace(key, 'main')}`;" for key, code in functions.items()])
  kernel_names = ', '.join([name for (name, _args, _global_size) in statements])
  kernel_calls = '\n    '.join([f"addComputePass(device, commandEncoder, piplines[{i}], [{', '.join(args)}], {global_size});" for i, (_name, args, global_size) in enumerate(statements) ])
  bufs =  '\n    '.join([f"const {buf[0]} = " + (f"createEmptyBuf(device, {buf[1]});" if buf[2] not in weights else f"createWeightBuf(device, {buf[1]}, getTensorBuffer(safetensor, metadata['{weights[buf[2]]}']))") + ";"  for buf in bufs.values()])

  prg = f"""const getTensorMetadata = (safetensorBuffer) => {{
  const metadataLength = Number(new DataView(safetensorBuffer.buffer).getBigUint64(0, true));
  const metadata = JSON.parse(new TextDecoder("utf8").decode(safetensorBuffer.subarray(8, 8 + metadataLength)));
  return Object.fromEntries(Object.entries(metadata).filter(([k, v]) => k !== "__metadata__").map(([k, v]) => [k, {{...v, data_offsets: v.data_offsets.map(x => 8 + metadataLength + x)}}]));
}};

const getTensorBuffer = (safetensorBuffer, tensorMetadata) => {{
  return safetensorBuffer.subarray(...tensorMetadata.data_offsets);
}}
  
const createEmptyBuf = (device, size) => {{
    return device.createBuffer({{size, usage: GPUBufferUsage.STORAGE | GPUBufferUsage.COPY_SRC | GPUBufferUsage.COPY_DST }});
}};

const createWeightBuf = (device, size, data) => {{
  const buf = device.createBuffer({{ mappedAtCreation: true, size, usage: GPUBufferUsage.STORAGE }});
  new Uint8Array(buf.getMappedRange()).set(data);
  buf.unmap();
  return buf;
}};

const addComputePass = (device, commandEncoder, pipeline, bufs, workgroup) => {{
  const bindGroup = device.createBindGroup({{layout: pipeline.getBindGroupLayout(0), entries: bufs.map((buffer, index) => ({{ binding: index, resource: {{ buffer }} }}))}});
  const passEncoder = commandEncoder.beginComputePass();
  passEncoder.setPipeline(pipeline);
  passEncoder.setBindGroup(0, bindGroup);
  passEncoder.dispatchWorkgroups(...workgroup);
  passEncoder.end();
}};

{kernel_code}
      
const setupNet = async (device, safetensor) => {{
    const metadata = getTensorMetadata(safetensor);

    {bufs}

    const gpuWriteBuffer = device.createBuffer({{size:input.size, usage: GPUBufferUsage.COPY_SRC | GPUBufferUsage.MAP_WRITE }});
    const gpuReadBuffer = device.createBuffer({{ size: outputs.size, usage: GPUBufferUsage.COPY_DST | GPUBufferUsage.MAP_READ }});

    const kernels = [{kernel_names}];
    const piplines = await Promise.all(kernels.map(name => device.createComputePipelineAsync({{layout: "auto", compute: {{ module: device.createShaderModule({{ code: name }}), entryPoint: "main" }}}})));

    return async (data) => {{
        await gpuWriteBuffer.mapAsync(GPUMapMode.WRITE);
        new Float32Array(gpuWriteBuffer.getMappedRange()).set(data);
        gpuWriteBuffer.unmap();

        const commandEncoder = device.createCommandEncoder();
        commandEncoder.copyBufferToBuffer(gpuWriteBuffer, 0, input, 0, gpuWriteBuffer.size);
        {kernel_calls}
        commandEncoder.copyBufferToBuffer(outputs, 0, gpuReadBuffer, 0, outputs.size);
        const gpuCommands = commandEncoder.finish();
        device.queue.submit([gpuCommands]);

        await gpuReadBuffer.mapAsync(GPUMapMode.READ);
        const resultBuffer = new Float32Array(gpuReadBuffer.size);
        resultBuffer.set(new Float32Array(gpuReadBuffer.getMappedRange()));
        gpuReadBuffer.unmap();
        return resultBuffer;
    }}
}}
"""

  with open(path.join(path.dirname(__file__), "net.js"), "w") as text_file:
    text_file.write(prg)
