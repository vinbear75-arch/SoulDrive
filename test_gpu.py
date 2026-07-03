import torch

# 1. 打印PyTorch版本
print("PyTorch版本:", torch.__version__)

# 2. 查看CUDA编译版本
print("PyTorch内置CUDA版本:", torch.version.cuda)

# 3. 判断CUDA是否可用（核心判断）
print("CUDA是否可用:", torch.cuda.is_available())

# 4. 查看GPU数量
print("GPU设备数量:", torch.cuda.device_count())

# 5. 查看当前GPU名称
if torch.cuda.is_available():
    print("GPU名称:", torch.cuda.get_device_name(0))
    # 把张量放到GPU测试
    x = torch.rand(1000, 1000).cuda()
    print("张量所在设备:", x.device)
else:
    print("未检测到可用GPU，当前只用CPU运行")