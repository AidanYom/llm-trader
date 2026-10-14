# The Lambda image's registry. Each image is tagged with the commit it was built from, and tags are
# immutable: Lambda pins a tag to its image digest at deploy, so a tag must always mean the same image.
resource "aws_ecr_repository" "trader" {
  name                 = "llm-trader"
  image_tag_mutability = "IMMUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }
}

resource "aws_ecr_lifecycle_policy" "trader" {
  repository = aws_ecr_repository.trader.name
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Keep the last 10 images"
      selection = {
        tagStatus   = "any"
        countType   = "imageCountMoreThan"
        countNumber = 10
      }
      action = { type = "expire" }
    }]
  })
}
