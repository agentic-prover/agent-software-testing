#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <math.h>
#include "lib/matheval.h"

#define MAX_INPUT_LEN 256

int yywrap() {return 1;}

int main (int argc, char **argv) {
  char buffer[MAX_INPUT_LEN];

  if (!fgets(buffer, MAX_INPUT_LEN, stdin)) {
    fprintf(stderr, "Error reading input.\n");
    exit(1);
  }

  buffer[strcspn(buffer, "\n")] = 0;

  void *f = evaluator_create(buffer);
  if (!f) {
    fprintf(stderr, "Invalid function input.\n");
    exit(1);
  }

  void *f_deriv = evaluator_derivative_x(f);
  if (!f_deriv) {
    fprintf(stderr, "Failed to compute derivative.\n");
    evaluator_destroy(f);
    exit(1);
  }

  double slope = evaluator_evaluate_x(f_deriv, 4.2);
  printf("f'(4.2) = %g\n", slope);

  evaluator_destroy(f);
  evaluator_destroy(f_deriv);
}
