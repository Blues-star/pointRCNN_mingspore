#include <cstdlib>
#include <torch/serialize/tensor.h>
#include <torch/extension.h>
#include <vector>
#include <cuda.h>
#include <cuda_runtime_api.h>
#include "ms_ext.h"
#include "torch/csrc/autograd/generated/variable_factories.h"
#include "torch/serialize/input-archive.h"

#define CHECK_CUDA(x) TORCH_CHECK(x.type().is_cuda(), #x, " must be a CUDAtensor ")
#define CHECK_CONTIGUOUS(x) TORCH_CHECK(x.is_contiguous(), #x, " must be contiguous ")
#define CHECK_INPUT(x) CHECK_CUDA(x);CHECK_CONTIGUOUS(x)

#define DIVUP(m,n) ((m) / (n) + ((m) % (n) > 0))

#define CHECK_ERROR(ans) { gpuAssert((ans), __FILE__, __LINE__); }
inline void gpuAssert(cudaError_t code, const char *file, int line, bool abort=true)
{
   if (code != cudaSuccess)
   {
      fprintf(stderr,"GPUassert: %s %s %d\n", cudaGetErrorString(code), file, line);
      if (abort) exit(code);
   }
}

const int THREADS_PER_BLOCK_NMS = sizeof(unsigned long long) * 8;


void boxesoverlapLauncher(const int num_a, const float *boxes_a, const int num_b, const float *boxes_b, float *ans_overlap);
void boxesioubevLauncher(const int num_a, const float *boxes_a, const int num_b, const float *boxes_b, float *ans_iou);
void nmsLauncher(const float *boxes, unsigned long long * mask, int boxes_num, float nms_overlap_thresh);
void nmsNormalLauncher(const float *boxes, unsigned long long * mask, int boxes_num, float nms_overlap_thresh);

int boxes_overlap_bev_gpu(at::Tensor boxes_a, at::Tensor boxes_b, at::Tensor ans_overlap){
    // params boxes_a: (N, 5) [x1, y1, x2, y2, ry]
    // params boxes_b: (M, 5) 
    // params ans_overlap: (N, M)
    
    CHECK_INPUT(boxes_a);
    CHECK_INPUT(boxes_b);
    CHECK_INPUT(ans_overlap);

    int num_a = boxes_a.size(0);
    int num_b = boxes_b.size(0);

    const float * boxes_a_data = boxes_a.data_ptr<float>();
    const float * boxes_b_data = boxes_b.data_ptr<float>();
    float * ans_overlap_data = ans_overlap.data_ptr<float>();

    boxesoverlapLauncher(num_a, boxes_a_data, num_b, boxes_b_data, ans_overlap_data);

    return 1;
}

extern "C" int ms_boxes_overlap_bev_gpu(int nparam, void **params, int *ndims, int64_t **shapes, const char **dtypes, void *stream, void *extra){
    auto tensors = get_torch_tensors(nparam, params, ndims, shapes, dtypes, c10::kCUDA);

    boxes_overlap_bev_gpu(tensors[0],tensors[1],tensors[2]);
    return 0;
}

int boxes_iou_bev_gpu(at::Tensor boxes_a, at::Tensor boxes_b, at::Tensor ans_iou){
    // params boxes_a: (N, 5) [x1, y1, x2, y2, ry]
    // params boxes_b: (M, 5) 
    // params ans_overlap: (N, M)
    
    CHECK_INPUT(boxes_a);
    CHECK_INPUT(boxes_b);
    CHECK_INPUT(ans_iou);

    int num_a = boxes_a.size(0);
    int num_b = boxes_b.size(0);

    const float * boxes_a_data = boxes_a.data_ptr<float>();
    const float * boxes_b_data = boxes_b.data_ptr<float>();
    float * ans_iou_data = ans_iou.data_ptr<float>();

    boxesioubevLauncher(num_a, boxes_a_data, num_b, boxes_b_data, ans_iou_data);

    return 1;
}

extern "C" int ms_boxes_iou_bev_gpu(int nparam, void **params, int *ndims, int64_t **shapes, const char **dtypes, void *stream, void *extra){
    auto tensors = get_torch_tensors(nparam, params, ndims, shapes, dtypes, c10::kCUDA);

    boxes_iou_bev_gpu(tensors[0],tensors[1],tensors[2]);
    return 0;
}


int nms_gpu(at::Tensor boxes, at::Tensor keep, float nms_overlap_thresh){
    // params boxes: (N, 5) [x1, y1, x2, y2, ry]
    // params keep: (N)

    CHECK_INPUT(boxes);
    CHECK_CONTIGUOUS(keep);

    int boxes_num = boxes.size(0);
    const float * boxes_data = boxes.data_ptr<float>();
    long * keep_data = keep.data_ptr<long>();
    //auto length = keep.element_size();
    std::vector<long> keep_data_cpu(boxes_num * sizeof(long));
    const int col_blocks = DIVUP(boxes_num, THREADS_PER_BLOCK_NMS);

    unsigned long long *mask_data = NULL;
    CHECK_ERROR(cudaMalloc((void**)&mask_data, boxes_num * col_blocks * sizeof(unsigned long long)));
    nmsLauncher(boxes_data, mask_data, boxes_num, nms_overlap_thresh);

    // unsigned long long mask_cpu[boxes_num * col_blocks];
    // unsigned long long *mask_cpu = new unsigned long long [boxes_num * col_blocks];
    std::vector<unsigned long long> mask_cpu(boxes_num * col_blocks);

    //    printf("boxes_num=%d, col_blocks=%d\n", boxes_num, col_blocks);
    CHECK_ERROR(cudaMemcpy(&mask_cpu[0], mask_data, boxes_num * col_blocks * sizeof(unsigned long long),
                           cudaMemcpyDeviceToHost));

    cudaFree(mask_data);

    unsigned long long remv_cpu[col_blocks];
    memset(remv_cpu, 0, col_blocks * sizeof(unsigned long long));
    // memset(keep_data_cpu,0,length*sizeof(long));
    int num_to_keep = 0;

    for (int i = 0; i < boxes_num; i++){
        int nblock = i / THREADS_PER_BLOCK_NMS;
        int inblock = i % THREADS_PER_BLOCK_NMS;

        if (!(remv_cpu[nblock] & (1ULL << inblock))){
            keep_data_cpu[num_to_keep++] = i;
            unsigned long long *p = &mask_cpu[0] + i * col_blocks;
            for (int j = nblock; j < col_blocks; j++){
                remv_cpu[j] |= p[j];
            }
        }
    }
    CHECK_ERROR(cudaMemcpy(&keep_data[0], &keep_data_cpu[0], boxes_num * sizeof(long),
                           cudaMemcpyHostToDevice));
    if ( cudaSuccess != cudaGetLastError() ) printf( "Error!\n" );
    return num_to_keep;
}

extern "C" int ms_nms_gpu(int nparam, void **params, int *ndims, int64_t **shapes, const char **dtypes, void *stream, void *extra){
    auto tensors = get_torch_tensors(nparam, params, ndims, shapes, dtypes, c10::kCUDA);
    float nms_overlap_thresh = tensors[2].item<float>();
    int ans = nms_gpu(tensors[0],tensors[1],nms_overlap_thresh);
    torch::Tensor ans_tensor = torch::full(1, ans);
    tensors[3].copy_(ans_tensor);
    return 0;
}


int nms_normal_gpu(at::Tensor boxes, at::Tensor keep, float nms_overlap_thresh){
    // params boxes: (N, 5) [x1, y1, x2, y2, ry]
    // params keep: (N)

    CHECK_INPUT(boxes);
    CHECK_CONTIGUOUS(keep);

    int boxes_num = boxes.size(0);
    const float * boxes_data = boxes.data_ptr<float>();
    long * keep_data = keep.data_ptr<long>();
    //auto length = keep.element_size();
    std::vector<long> keep_data_cpu(boxes_num * sizeof(long));
    const int col_blocks = DIVUP(boxes_num, THREADS_PER_BLOCK_NMS);

    unsigned long long *mask_data = NULL;
    CHECK_ERROR(cudaMalloc((void**)&mask_data, boxes_num * col_blocks * sizeof(unsigned long long)));
    nmsNormalLauncher(boxes_data, mask_data, boxes_num, nms_overlap_thresh);

    // unsigned long long mask_cpu[boxes_num * col_blocks];
    // unsigned long long *mask_cpu = new unsigned long long [boxes_num * col_blocks];
    std::vector<unsigned long long> mask_cpu(boxes_num * col_blocks);

    //    printf("boxes_num=%d, col_blocks=%d\n", boxes_num, col_blocks);
    CHECK_ERROR(cudaMemcpy(&mask_cpu[0], mask_data, boxes_num * col_blocks * sizeof(unsigned long long),
                           cudaMemcpyDeviceToHost));

    cudaFree(mask_data);

    unsigned long long remv_cpu[col_blocks];
    memset(remv_cpu, 0, col_blocks * sizeof(unsigned long long));

    int num_to_keep = 0;

    for (int i = 0; i < boxes_num; i++){
        int nblock = i / THREADS_PER_BLOCK_NMS;
        int inblock = i % THREADS_PER_BLOCK_NMS;

        if (!(remv_cpu[nblock] & (1ULL << inblock))){
            keep_data_cpu[num_to_keep++] = i;
            unsigned long long *p = &mask_cpu[0] + i * col_blocks;
            for (int j = nblock; j < col_blocks; j++){
                remv_cpu[j] |= p[j];
            }
        }
    }
    CHECK_ERROR(cudaMemcpy(&keep_data[0], &keep_data_cpu[0], boxes_num * sizeof(long),
                           cudaMemcpyHostToDevice));
    if ( cudaSuccess != cudaGetLastError() ) printf( "Error!\n" );

    return num_to_keep;
}
extern "C" int ms_nms_normal_gpu(int nparam, void **params, int *ndims, int64_t **shapes, const char **dtypes, void *stream, void *extra){
    auto tensors = get_torch_tensors(nparam, params, ndims, shapes, dtypes, c10::kCUDA);
    float nms_overlap_thresh = tensors[2].item<float>();
    int ans = nms_normal_gpu(tensors[0],tensors[1],nms_overlap_thresh);
    torch::Tensor ans_tensor = torch::full(1, ans);
    tensors[3].copy_(ans_tensor);
    return 0;
}


PYBIND11_MODULE(TORCH_EXTENSION_NAME, m) {
  m.def("boxes_overlap_bev_gpu", &boxes_overlap_bev_gpu, "oriented boxes overlap");
  m.def("boxes_iou_bev_gpu", &boxes_iou_bev_gpu, "oriented boxes iou");
  m.def("nms_gpu", &nms_gpu, "oriented nms gpu");
  m.def("nms_normal_gpu", &nms_normal_gpu, "nms gpu");
}

