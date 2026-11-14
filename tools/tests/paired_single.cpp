#include "runtime/cpu_context.h"
#include <iostream>

int main() {
  nwii::runtime::CPUContext ctx;
  unsigned failures = 0;
  for (unsigned d=0; d<4; ++d)
    for (unsigned a=0; a<4; ++a)
      for (unsigned c=0; c<4; ++c)
        for (unsigned b=0; b<4; ++b) {
          auto reset = [&]() {
            for (unsigned i=0; i<4; ++i) {
              ctx.fpr[i] = i + 2;
              ctx.ps1[i] = i + 7;
            }
          };
          reset();
          const double mul0 = ctx.fpr[a] * ctx.fpr[c];
          const double mul1 = ctx.ps1[a] * ctx.fpr[c];
          ctx.ps_muls0(d,a,c);
          failures += ctx.fpr[d]!=mul0 || ctx.ps1[d]!=mul1;
          reset();
          const double add0 = std::fma(ctx.fpr[a],ctx.fpr[c],ctx.fpr[b]);
          const double add1 = std::fma(ctx.ps1[a],ctx.fpr[c],ctx.ps1[b]);
          ctx.ps_madds0(d,a,c,b);
          failures += ctx.fpr[d]!=add0 || ctx.ps1[d]!=add1;
        }
  std::cout << "Paired-single alias checks: 512 cases, " << failures << " failures\n";
  return failures ? 1 : 0;
}
